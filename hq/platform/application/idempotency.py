"""Durable replay semantics shared by machine and human command transports."""

import hashlib
import json
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from hq.platform.core.models import IdempotencyRecord

from .integration_specs import CURRENT_API_VERSION, IDEMPOTENCY_KEY_LENGTH, IDEMPOTENCY_KEY_PATTERN

KEY = re.compile(IDEMPOTENCY_KEY_PATTERN)
KEY_SHAPE = f"A retry key is 1-{IDEMPOTENCY_KEY_LENGTH} URL-safe characters."


class _KeyProblem(ValueError):
    """Carries a client-facing sentence rather than relying on ``str``."""

    def __init__(self, reason: str = "", *args: object) -> None:
        super().__init__(reason, *args)
        self.reason = reason


class InvalidIdempotencyKey(_KeyProblem):
    pass


class IdempotencyConflict(_KeyProblem):
    pass


def validate_key(value: str) -> str:
    if not KEY.fullmatch(value):
        raise InvalidIdempotencyKey(KEY_SHAPE)
    return value


def request_fingerprint(name: str, envelope: dict[str, Any]) -> str:
    """The request a key was first used for: the capability, its command, target and stale-write token."""

    canonical = json.dumps(
        {"api_version": CURRENT_API_VERSION, "capability": name, "request": envelope},
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def execute_form_once(
    *, actor: str, key: str, write: str, form: dict[str, Any], operation: Callable[[], dict[str, Any]]
) -> tuple[dict[str, Any], bool]:
    """``execute_once`` for a form view whose write is no capability (``writes``' mixins).

    ``write`` names the write, as a capability's name does, so the same key
    and body sent to another form is a conflict, never that form's answer;
    ``form`` is the post as its fingerprint reads it.
    """

    return execute_once(
        actor=actor, key=key, request_sha256=request_fingerprint(f"form:{write}", form), operation=operation
    )


def form_answer(*, actor: str, key: str, write: str, form: dict[str, Any]) -> dict[str, Any] | None:
    """The answer ``key`` committed for this same post, or None while it has none.

    For a form that must answer a resend before it can be validated again:
    one whose first post created what its validation now refuses as taken. A
    key used for another request is ``execute_once``'s conflict to report.
    """

    record = IdempotencyRecord.objects.filter(
        actor_sha256=_actor_sha256(actor),
        key=key,
        request_sha256=request_fingerprint(f"form:{write}", form),
        expires_at__gt=timezone.now(),
    ).first()
    return None if record is None else record.response


def _actor_sha256(actor: str) -> str:
    return hashlib.sha256(actor.encode("utf-8")).hexdigest()


def execute_once(
    *,
    actor: str,
    key: str,
    request_sha256: str,
    operation: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """``(answer, replayed)``: run once, or replay the committed answer for the same request.

    Only an answer that ran (``ok``) is kept; one that did not (a refusal, a
    call held for a person) frees its key while what the operation wrote
    commits, so the same key runs once the cause has gone.
    """

    actor_sha256 = _actor_sha256(actor)
    now = timezone.now()
    ttl = timedelta(seconds=settings.SEVERINO_API_IDEMPOTENCY_TTL_SECONDS)
    # The reservation and the operation share a transaction, so a crashed
    # process leaves neither a partial domain write nor a poisoned pending key,
    # and concurrent retries converge on one committed record on both SQLite
    # and PostgreSQL.
    with transaction.atomic():
        IdempotencyRecord.objects.filter(expires_at__lte=now).delete()
        record, created = IdempotencyRecord.objects.get_or_create(
            actor_sha256=actor_sha256,
            key=key,
            defaults={
                "actor": actor[:255],
                "request_sha256": request_sha256,
                "expires_at": now + ttl,
            },
        )
        if not created:
            record = IdempotencyRecord.objects.select_for_update().get(pk=record.pk)
            if record.request_sha256 != request_sha256:
                raise IdempotencyConflict("This retry key was already used for a different request.")
            if record.response is None:
                raise RuntimeError("An idempotency record has no committed response.")
            return record.response, True

        response = operation()
        if not response.get("ok", False):
            record.delete()
            return response, False
        record.response = response
        record.status_code = 200
        record.save(update_fields=("response", "status_code"))
        return response, False
