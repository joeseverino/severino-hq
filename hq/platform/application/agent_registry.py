"""The agents HQ knows about, learned from the tokens they present, and the
tailnet devices they call from (``agent_devices``).

A first sighting and any change to an identity's grant are audited. Observation
never blocks a request.
"""

import logging
from collections.abc import Callable
from datetime import timedelta
from functools import partial

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.dispatch import Signal
from django.utils import timezone

from hq.platform.core.audit import record_event
from hq.platform.core.models import AgentIdentity, AgentSignIn, AuditLog

from . import agent_devices
from .doors import Attribution
from .linked_accounts import SIGN_IN
from .security import AGENT_SURFACES, Principal, agent_subject, subject_parts
from .ui import counted
lazy from .tailnet import Device

logger = logging.getLogger("severino.agents")

AUDIT_LABEL = "Agent identity"

# last_seen is kept to this precision so routine traffic reads without writing.
TOUCH_INTERVAL = timedelta(minutes=10)

# Sent once a change commits that alters which tools an agent is listed, with
# ``aimed``, which says of an open stream's principal whether the change is its
# own. The doors that hold a stream open receive it; the application never
# names them (import-linter's layering contract).
TOOLS_CHANGED = Signal()
# Sent once a person's change to an agent's brakes commits, so a stream that
# was held back an event while refused is given it once it may act again.
STANDING_CHANGED = Signal()

type Aimed = Callable[[Principal], bool]


def tools_changed(aimed: Aimed) -> None:
    """Say, once the change commits, that the tools listed to the callers ``aimed`` admits changed."""

    # Robust: a change that committed does not fail for a notice that could not be given.
    transaction.on_commit(partial(TOOLS_CHANGED.send, sender=Principal, aimed=aimed), robust=True)


def persons_tools_changed(user) -> None:
    """Say that the tools listed to every agent a person signed in changed: their role did."""

    keys = frozenset(user.linked_accounts.filter(provider=SIGN_IN).values_list("login", flat=True))
    if keys:
        tools_changed(lambda principal: principal.person_key in keys)


def standing_changed() -> None:
    """Say, once it commits, that a person changed an agent's brakes."""

    transaction.on_commit(partial(STANDING_CHANGED.send, sender=Principal), robust=True)


def observe(principal: Principal | None, device: Device | None = None) -> None:
    """Record the agent ``principal`` names, and the ``device`` its call came
    from when it is an agent's call placed on one."""

    # Only token principals carry a grant; the operator and the CLI do not.
    if principal is None or not principal.granted:
        return
    try:
        row, person = _observe(principal)
        # A person's own client needs no device of its own, and a token whose
        # person HQ cannot find has no person to approve one.
        if device is not None and principal.interface in AGENT_SURFACES and (person or not principal.person_key):
            agent_devices.observe(row, person, device, interface=principal.interface)
    except Exception:  # noqa: BLE001 - observation never blocks a request
        logger.warning(
            "Agent identity not recorded: %s",
            principal.actor,
            extra={"event": "agents.identity.unrecorded"},
        )


def identity_of(subject: str) -> QuerySet[AgentIdentity]:
    """The row ``subject`` names (``security.agent_subject``): a client as a
    person, or a machine's client whole, whatever its id holds."""

    client_id, person_key = subject_parts(subject)
    return AgentIdentity.objects.filter(
        Q(client_id=subject, person_key="") | Q(client_id=client_id, person_key=person_key)
    )


def _observe(principal: Principal) -> tuple[AgentIdentity, object]:
    now = timezone.now()
    granted = sorted(principal.granted)
    signs_in = AgentSignIn.PERSON if principal.person_key else AgentSignIn.MACHINE
    person = _person(principal.person_key)
    keyed = {"client_id": principal.actor, "person_key": principal.person_key}
    row = AgentIdentity.objects.filter(**keyed).first()
    if row is None:
        try:
            with transaction.atomic():
                row = AgentIdentity.objects.create(
                    **keyed,
                    interfaces=[principal.interface],
                    granted=granted,
                    signs_in=signs_in,
                    person=person,
                    last_seen=now,
                )
        except IntegrityError:
            row = AgentIdentity.objects.get(**keyed)
        else:
            record_event(
                action=AuditLog.Action.CREATED,
                obj=row,
                type_label=AUDIT_LABEL,
                message=(
                    f"First seen: {Attribution(str(row), principal.interface).asker}, "
                    f"granted {counted(len(granted), 'permission')}"
                ),
                metadata={
                    "actor": principal.actor,
                    "interface": principal.interface,
                    **_device(principal),
                    "signs_in": signs_in,
                    "person": "" if person is None else person.get_username(),
                },
            )
            return row, person

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
        # Whether a tool is listed is the grant's to say, so a stream this
        # agent holds open is told to list again.
        subject = agent_subject(principal)
        tools_changed(lambda listening: agent_subject(listening) == subject)
        record_event(
            action=AuditLog.Action.UPDATED,
            obj=row,
            type_label=AUDIT_LABEL,
            message=_grant_change(principal.actor, added, removed),
            metadata={
                "actor": principal.actor,
                "interface": principal.interface,
                **_device(principal),
                "added": added,
                "removed": removed,
            },
        )
    if changed or now - row.last_seen >= TOUCH_INTERVAL:
        row.last_seen = now
        row.save(update_fields=[*changed, "last_seen"])
    return row, person


def _device(principal: Principal) -> dict[str, str]:
    """The device a call came from, for the events its admission records
    before the door binds its caller."""

    return {"device": principal.device} if principal.device else {}


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
            **_device(principal),
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
