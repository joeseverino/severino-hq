"""Durable replay semantics for retryable machine writes."""

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

from .models import IdempotencyRecord

KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class InvalidIdempotencyKey(ValueError):
    pass


class IdempotencyConflict(ValueError):
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

    The reservation and the domain operation share a transaction. A crashed
    process therefore leaves neither a partial domain write nor a poisoned
    pending key. ``get_or_create`` also makes concurrent retries converge on
    the one committed record on both SQLite and PostgreSQL.
    """

    actor_sha256 = hashlib.sha256(actor.encode("utf-8")).hexdigest()
    now = timezone.now()
    ttl = timedelta(seconds=settings.SEVERINO_API_IDEMPOTENCY_TTL_SECONDS)
    with transaction.atomic():
        # Keep storage proportional to the retry window without introducing a
        # scheduler solely to maintain transport bookkeeping.
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

        if not created:
            # A record only commits after its response does. Null here would
            # mean database corruption or a manually modified row, never a
            # legitimate in-flight request.
            if record.response is None or record.status_code is None:
                raise RuntimeError("An idempotency record has no committed response.")
            return record.response, record.status_code, True

        response, status_code = operation()
        record.response = response
        record.status_code = status_code
        record.save(update_fields=("response", "status_code"))
        return response, status_code, False
