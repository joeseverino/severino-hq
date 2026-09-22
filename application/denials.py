"""Refusals, recorded in the audit log.

An authenticated caller's refusal gets its own row. A rejected credential proves
nothing about its sender, so those are counted per surface, source and reason
per minute. The credential itself is never recorded, and recording never blocks
the refusal.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from core.audit import record_event
from core.models import AuditLog

logger = logging.getLogger("severino.denials")

LABEL = "Access denied"
COALESCE_WINDOW = timedelta(seconds=60)


def record_denial(
    *,
    interface: str,
    reason: str,
    actor: str = "",
    capability: str = "",
    source: str = "",
    detail: str = "",
    authenticated: bool = True,
) -> None:
    metadata = {
        "interface": interface,
        "actor": actor,
        "reason": reason,
        "capability": capability,
        "source": source,
        "authenticated": authenticated,
    }
    try:
        if not authenticated and _coalesce(interface, reason, source):
            return
        record_event(
            action=AuditLog.Action.DENIED,
            type_label=LABEL,
            message=_message(reason, actor=actor, capability=capability, detail=detail),
            metadata={**metadata, "count": 1},
        )
    except Exception:  # noqa: BLE001 - best effort by contract
        logger.warning(
            "Refusal not recorded: %s on %s", reason, interface,
            extra={"event": "audit.denial.unrecorded"},
        )


def _coalesce(interface: str, reason: str, source: str) -> bool:
    recent = (
        AuditLog.objects.filter(
            action=AuditLog.Action.DENIED,
            object_type=LABEL,
            created_at__gte=timezone.now() - COALESCE_WINDOW,
            metadata__authenticated=False,
            metadata__interface=interface,
            metadata__reason=reason,
            metadata__source=source,
        )
        .order_by("-id")
        .first()
    )
    if recent is None:
        return False
    recent.metadata = {
        **recent.metadata,
        "count": int(recent.metadata.get("count", 1)) + 1,
        "last_at": timezone.now().isoformat(),
    }
    recent.save(update_fields=["metadata"])
    return True


def _message(reason: str, *, actor: str, capability: str, detail: str) -> str:
    who = actor or "An unauthenticated caller"
    what = f" {capability}" if capability else ""
    said = f"{who} was refused{what}: {reason.replace('_', ' ')}"
    return f"{said}. {detail}".strip() if detail else said
