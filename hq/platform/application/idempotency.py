"""Durable replay semantics shared by machine and human command transports."""

import hashlib
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from hq.platform.core.digests import digest
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

    return digest({"api_version": CURRENT_API_VERSION, "capability": name, "request": envelope})


def answered(*, actor: str, key: str) -> dict[str, Any] | None:
    """The answer ``actor``'s ``key`` committed, or None while it has none.

    For a form that must answer a resend before it can be validated again:
    one whose first post created what its validation now refuses as taken.
    A form draws a fresh key, so the key alone names the post it was drawn for.
    """

    record = IdempotencyRecord.objects.filter(
        actor_sha256=_actor_sha256(actor), key=key, expires_at__gt=timezone.now()
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
    call held for a person) keeps no key while what the operation wrote
    commits, so the same key runs once the cause has gone.
    """

    actor_sha256 = _actor_sha256(actor)
    now = timezone.now()
    ttl = timedelta(seconds=settings.SEVERINO_API_IDEMPOTENCY_TTL_SECONDS)
    held = IdempotencyRecord.objects.filter(actor_sha256=actor_sha256, key=key)
    # The answer is kept in the operation's transaction once it ran, so a
    # crash leaves neither a partial write nor a poisoned key. Its caller's
    # transaction is serialized (``database.serialized``), so two retries
    # converge on one committed record and one committed act.
    with transaction.atomic():
        if (first := _first(held, request_sha256, now)) is not None:
            return first, True
        response = operation()
        if not response.get("ok", False):
            return response, False
        IdempotencyRecord.objects.filter(expires_at__lte=now).delete()
        IdempotencyRecord.objects.create(
            actor_sha256=actor_sha256,
            key=key,
            actor=actor[:255],
            request_sha256=request_sha256,
            expires_at=now + ttl,
            response=response,
        )
        return response, False


def _first(held: Any, request_sha256: str, now: Any) -> dict[str, Any] | None:
    """The answer the key committed before, unexpired, or None; a key first used for another request is refused."""

    record = held.filter(expires_at__gt=now).first()
    if record is None:
        return None
    if record.request_sha256 != request_sha256:
        raise IdempotencyConflict("This retry key was already used for a different request.")
    return record.response
