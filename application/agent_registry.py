"""The agents HQ knows about, learned from the tokens they present.

A first sighting and any change to an identity's grant are audited. Observation
never blocks a request.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.audit import record_event
from core.models import AgentIdentity, AuditLog

from .security import Principal

logger = logging.getLogger("severino.agents")

AUDIT_LABEL = "Agent identity"

# last_seen is kept to this precision so routine traffic reads without writing.
TOUCH_INTERVAL = timedelta(minutes=10)


def observe(principal: Principal | None) -> None:
    # Only token principals carry a grant; the operator, CLI and shared bearer do not.
    if principal is None or not principal.granted:
        return
    try:
        _observe(principal)
    except Exception:  # noqa: BLE001 - observation never blocks a request
        logger.warning(
            "Agent identity not recorded: %s", principal.actor,
            extra={"event": "agents.identity.unrecorded"},
        )


def _observe(principal: Principal) -> None:
    now = timezone.now()
    granted = sorted(principal.granted)
    row = AgentIdentity.objects.filter(client_id=principal.actor).first()
    if row is None:
        try:
            with transaction.atomic():
                row = AgentIdentity.objects.create(
                    client_id=principal.actor,
                    interfaces=[principal.interface],
                    granted=granted,
                    last_seen=now,
                )
        except IntegrityError:
            row = AgentIdentity.objects.get(client_id=principal.actor)
        else:
            record_event(
                action=AuditLog.Action.CREATED,
                obj=row,
                type_label=AUDIT_LABEL,
                message=(
                    f"First seen: {principal.actor}, over {principal.interface}, "
                    f"granted {len(granted)} permission{'s' if len(granted) != 1 else ''}"
                ),
                metadata={"actor": principal.actor, "interface": principal.interface},
            )
            return

    changed: list[str] = []
    if principal.interface not in row.interfaces:
        row.interfaces = sorted({*row.interfaces, principal.interface})
        changed.append("interfaces")
    if granted != row.granted:
        added = sorted(set(granted) - set(row.granted))
        removed = sorted(set(row.granted) - set(granted))
        row.granted = granted
        changed.append("granted")
        record_event(
            action=AuditLog.Action.UPDATED,
            obj=row,
            type_label=AUDIT_LABEL,
            message=_grant_change(principal.actor, added, removed),
            metadata={
                "actor": principal.actor,
                "interface": principal.interface,
                "added": added,
                "removed": removed,
            },
        )
    if changed or now - row.last_seen >= TOUCH_INTERVAL:
        row.last_seen = now
        row.save(update_fields=[*changed, "last_seen"])


def _grant_change(actor: str, added: list[str], removed: list[str]) -> str:
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    return f"Grant for {actor} changed: " + "; ".join(parts)
