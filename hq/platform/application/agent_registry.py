"""The agents HQ knows about, learned from the tokens they present.

A first sighting and any change to an identity's grant are audited. Observation
never blocks a request.
"""

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from hq.platform.core.audit import record_event
from hq.platform.core.models import AgentIdentity, AgentSignIn, AuditLog

from .doors import door_words
from .linked_accounts import SIGN_IN
from .security import Principal
from .ui import counted

logger = logging.getLogger("severino.agents")

AUDIT_LABEL = "Agent identity"

# last_seen is kept to this precision so routine traffic reads without writing.
TOUCH_INTERVAL = timedelta(minutes=10)


def observe(principal: Principal | None) -> None:
    # Only token principals carry a grant; the operator and the CLI do not.
    if principal is None or not principal.granted:
        return
    try:
        _observe(principal)
    except Exception:  # noqa: BLE001 - observation never blocks a request
        logger.warning(
            "Agent identity not recorded: %s",
            principal.actor,
            extra={"event": "agents.identity.unrecorded"},
        )


def _observe(principal: Principal) -> None:
    now = timezone.now()
    granted = sorted(principal.granted)
    signs_in = AgentSignIn.PERSON if principal.person_key else AgentSignIn.MACHINE
    person = _person(principal.person_key)
    row = AgentIdentity.objects.filter(client_id=principal.actor).first()
    if row is None:
        try:
            with transaction.atomic():
                row = AgentIdentity.objects.create(
                    client_id=principal.actor,
                    interfaces=[principal.interface],
                    granted=granted,
                    signs_in=signs_in,
                    person=person,
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
                    f"First seen: {principal.actor}, {door_words(principal.interface)}, "
                    f"granted {counted(len(granted), 'permission')}"
                ),
                metadata={
                    "actor": principal.actor,
                    "interface": principal.interface,
                    "signs_in": signs_in,
                    "person": "" if person is None else person.get_username(),
                },
            )
            return

    changed: list[str] = []
    if principal.interface not in row.interfaces:
        row.interfaces = sorted({*row.interfaces, principal.interface})
        changed.append("interfaces")
    if signs_in != row.signs_in:
        row.signs_in = signs_in
        changed.append("signs_in")
    if (None if person is None else person.pk) != row.person_id:
        changed.append("person")
        _record_person(row, principal, person)
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


def _person(person_key: str):
    """The person whose sign-in key a token names, or None for a machine's own token."""

    if not person_key:
        return None
    users = get_user_model()
    return users._default_manager.filter(linked_accounts__provider=SIGN_IN, linked_accounts__login=person_key).first()


def _record_person(row: AgentIdentity, principal: Principal, person) -> None:
    before = row.person
    row.person = person
    named = "nobody" if person is None else str(person)
    record_event(
        action=AuditLog.Action.UPDATED,
        obj=row,
        type_label=AUDIT_LABEL,
        message=f"{principal.actor} now acts for {named}" + (f", was {before}" if before is not None else ""),
        metadata={
            "actor": principal.actor,
            "interface": principal.interface,
            "person": "" if person is None else person.get_username(),
            "was": "" if before is None else before.get_username(),
        },
    )


def _grant_change(actor: str, added: list[str], removed: list[str]) -> str:
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    return f"Grant for {actor} changed: " + "; ".join(parts)
