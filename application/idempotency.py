"""Durable replay semantics shared by machine and human command transports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import hashlib
import json
import re
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from hq_api.models import IdempotencyRecord


KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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
        raise InvalidIdempotencyKey(
            "Idempotency-Key must be 1-128 URL-safe characters."
        )
    return value


def request_fingerprint(name: str, envelope: dict[str, Any], *, api_version: int) -> str:
    canonical = json.dumps(
        {"api_version": api_version, "capability": name, "request": envelope},
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def execute_once(
    *,
    actor: str,
    key: str,
    request_sha256: str,
    operation: Callable[[], tuple[dict[str, Any], int]],
) -> tuple[dict[str, Any], int, bool]:
    """Run once, or replay the committed response for the same request.

    The reservation and domain operation share a transaction. A crashed
    process therefore leaves neither a partial domain write nor a poisoned
    pending key. Concurrent retries converge on one committed record on both
    SQLite and PostgreSQL.
    """

    actor_sha256 = hashlib.sha256(actor.encode("utf-8")).hexdigest()
    now = timezone.now()
    ttl = timedelta(seconds=settings.SEVERINO_API_IDEMPOTENCY_TTL_SECONDS)
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
                raise IdempotencyConflict(
                    "This Idempotency-Key was already used for a different request."
                )
            if record.response is None or record.status_code is None:
                raise RuntimeError("An idempotency record has no committed response.")
            return record.response, record.status_code, True

        response, status_code = operation()
        record.response = response
        record.status_code = status_code
        record.save(update_fields=("response", "status_code"))
        return response, status_code, False
