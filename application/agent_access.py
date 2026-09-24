"""The operator's switch that pauses every agent, on /mcp/ and on the machine API."""

from __future__ import annotations

import logging

from django.db import DatabaseError, transaction
from django.utils import timezone

from application.security import AuthorizationError, Principal, is_interactive
from core.audit import record_event
from core.models import AgentAccess, AuditLog

logger = logging.getLogger("severino.agents")

AUDIT_LABEL = "Agent access"


def agents_paused() -> bool:
    """Read on every agent request (MCP and /api/), never cached. Fails closed."""

    try:
        row = AgentAccess.objects.filter(pk=1).only("paused").first()
    except DatabaseError:
        logger.warning("Agent access unreadable; refusing agents.", extra={"event": "agents.access.unreadable"})
        return True
    return bool(row and row.paused)


def set_agents_paused(paused: bool, *, principal: Principal, user) -> AgentAccess:
    """Set, not toggle, so a repeated request cannot undo itself. Operators only."""

    if not is_interactive(principal):
        raise AuthorizationError(
            f"{principal.interface} principal {principal.actor!r} cannot change agent access."
        )

    with transaction.atomic():
        row, _ = AgentAccess.objects.select_for_update().get_or_create(pk=1)
        if row.paused == paused:
            return row
        row.paused = paused
        row.changed_by = user
        row.changed_at = timezone.now()
        row.save(update_fields=["paused", "changed_by", "changed_at"])
        record_event(
            action=AuditLog.Action.UPDATED,
            obj=row,
            type_label=AUDIT_LABEL,
            message="Agents paused" if paused else "Agents resumed",
            user=user,
            required=True,
        )
    return row
