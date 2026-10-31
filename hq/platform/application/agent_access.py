"""The operator's switch that pauses every agent, on /mcp/ and on the machine API."""

import logging
from dataclasses import dataclass
from typing import Annotated, Any

from django.db import DatabaseError, transaction
from django.utils import timezone

from hq.platform.application.security import Principal, person
from hq.platform.core.audit import record_event
from hq.platform.core.models import AgentAccess, AuditLog

from .command_fields import Describe

logger = logging.getLogger("severino.agents")

AUDIT_LABEL = "Agent access"

SET_AGENT_ACCESS = "agents.access.set"


def agents_paused() -> bool:
    """Read on every agent request (MCP and /api/), never cached. Fails closed."""

    try:
        row = AgentAccess.objects.filter(pk=1).only("paused").first()
    except DatabaseError:
        logger.warning("Agent access unreadable; refusing agents.", extra={"event": "agents.access.unreadable"})
        return True
    return bool(row and row.paused)


@dataclass(frozen=True, slots=True)
class AgentAccessCommand:
    """Whether every agent is paused."""

    paused: Annotated[bool, Describe("Whether every agent is paused.")]


def set_agents_paused(command: AgentAccessCommand, *, principal: Principal) -> dict[str, Any]:
    """Set, not toggle, so a repeated request cannot undo itself. People only."""

    user = person(principal)
    paused = command.paused
    with transaction.atomic():
        row, _ = AgentAccess.objects.select_for_update().get_or_create(pk=1)
        if row.paused == paused:
            return {"ok": True, "paused": paused}
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
    return {"ok": True, "paused": paused}
