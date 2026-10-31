"""What an operator wants at the top, and nothing else about it.

A pin is an ordering preference, so it is read as a set of keys and applied
where things are listed. It never reaches a spec, a generation or the
controller: the world does not change because somebody starred a domain.
"""

from dataclasses import dataclass, field
from typing import Annotated, Any

from django.core.exceptions import ValidationError
from django.db import transaction
lazy from django.db.models import Max

from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog, Pin

from .command_fields import Describe
from .projection import read_once
from .security import Principal, person

DOMAIN = "domain"
# A service, kept at the top of the list that shows every hostname HQ knows.
# Most of that list is infrastructure an operator reads once a quarter, and
# mixing the handful they open daily into it makes the page a search rather
# than a dashboard.
SERVICE = "service"
# A link on the dashboard's outward panel. Everything HQ can reach is offered
# there and most of it is not what an operator wants a shortcut to, so the panel
# shows what has been chosen and falls back to everything when nothing has.
DASHBOARD_LINK = "dashboard_link"
KINDS = (DOMAIN, SERVICE, DASHBOARD_LINK)

# The capabilities a person keeps their pins through.
PIN = "pin.toggle"
MOVE = "pin.move"
CHOOSE = "pin.choose"
AUDIT_LABEL = "Pin"


@dataclass(frozen=True, slots=True)
class PinCommand:
    kind: Annotated[str, Describe("What type of thing to pin.")]
    key: Annotated[str, Describe("Which one.")]


@dataclass(frozen=True, slots=True)
class MoveCommand:
    kind: Annotated[str, Describe("What type of pin to move.")]
    key: Annotated[str, Describe("Which pin.")]
    up: Annotated[bool, Describe("Move it up; otherwise down.")] = False


@dataclass(frozen=True, slots=True)
class ChooseCommand:
    kind: Annotated[str, Describe("What type of pins to set.")]
    keys: Annotated[list[str], Describe("Every pin of that type, in order.")] = field(default_factory=list)


def ordered(user, target_kind: str) -> tuple[str, ...]:
    """Pinned keys in the order the operator put them, lowercased.

    A tuple rather than a set, because the order is the answer. `pinned` stays
    a set for the far more common question of whether one key is in there.
    """

    if user is None or not user.is_authenticated:
        return ()
    return read_once(
        f"pins:{user.pk}:{target_kind}",
        lambda: tuple(
            key.lower()
            for key in Pin.objects.filter(user=user, target_kind=target_kind).values_list("target_key", flat=True)
        ),
    )


def pinned(user, target_kind: str) -> frozenset[str]:
    """Keys this operator has pinned, derived from the canonical ordered read."""

    return frozenset(ordered(user, target_kind))


def toggle(user, target_kind: str, target_key: str) -> bool:
    """Pin or unpin, returning whether it is pinned afterwards.

    A new pin lands at the end. Anywhere else and pinning something would
    silently move everything the operator had already arranged.
    """

    key = str(target_key).strip().lower()
    if not key:
        return False
    existing = Pin.objects.filter(user=user, target_kind=target_kind, target_key=key)
    if existing.exists():
        existing.delete()
        return False
    Pin.objects.create(
        user=user,
        target_kind=target_kind,
        target_key=key,
        position=_next_position(user, target_kind),
    )
    return True


def _next_position(user, target_kind: str) -> int:
    highest = Pin.objects.filter(user=user, target_kind=target_kind).aggregate(highest=Max("position"))["highest"]
    return 0 if highest is None else highest + 1


def move(user, target_kind: str, target_key: str, delta: int) -> None:
    """Shift one pin up or down among its neighbors.

    Implemented as a swap with the adjacent pin rather than by rewriting every
    position, so two operators reordering different pairs do not clobber each
    other's work.
    """

    if user is None or not user.is_authenticated or delta not in (-1, 1):
        return
    keys = list(ordered(user, target_kind))
    key = str(target_key).strip().lower()
    if key not in keys:
        return
    index = keys.index(key)
    swap = index + delta
    if not 0 <= swap < len(keys):
        return
    keys[index], keys[swap] = keys[swap], keys[index]
    reorder(user, target_kind, keys)


def reorder(user, target_kind: str, keys) -> None:
    """Renumber the pins of one kind into the given order.

    Only keys already pinned are honored: an order is a statement about what
    is pinned, and letting it create pins would make reordering a way to pin
    things without meaning to.
    """

    if user is None or not user.is_authenticated:
        return
    known = set(ordered(user, target_kind))
    wanted = [key for key in (str(item).strip().lower() for item in keys) if key in known]
    rows = {row.target_key.lower(): row for row in Pin.objects.filter(user=user, target_kind=target_kind)}
    changed = []
    for position, key in enumerate(wanted):
        row = rows.get(key)
        if row is not None and row.position != position:
            row.position = position
            changed.append(row)
    if changed:
        Pin.objects.bulk_update(changed, ["position"])


def replace(user, target_kind: str, keys) -> None:
    """Set exactly which keys are pinned for one kind.

    A chooser answers with the whole set, so applying it as toggles would depend
    on what was already stored and drift the moment two tabs disagree.
    """

    if user is None or not user.is_authenticated:
        return
    wanted = {str(key).strip().lower() for key in keys if str(key).strip()}
    Pin.objects.filter(user=user, target_kind=target_kind).exclude(target_key__in=wanted).delete()
    existing = pinned(user, target_kind)
    start = _next_position(user, target_kind)
    Pin.objects.bulk_create(
        Pin(user=user, target_kind=target_kind, target_key=key, position=start + offset)
        for offset, key in enumerate(sorted(wanted - existing))
    )


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValidationError({"kind": ValidationError("Not a type of pin.", code="invalid_choice")})
    return kind


def _key(key: str) -> str:
    if not key.strip():
        raise ValidationError({"key": ValidationError("Name what to pin.", code="required")})
    return key


def _audit(user, message: str, **metadata: Any) -> None:
    record_event(
        action=AuditLog.Action.UPDATED,
        type_label=AUDIT_LABEL,
        message=message,
        metadata=metadata,
        user=user,
        required=True,
    )


@transaction.atomic
def pin(command: PinCommand, *, principal: Principal) -> dict[str, Any]:
    """Pin or unpin one thing for the person asking: the ``pin.toggle`` capability."""

    kind, key = _kind(command.kind), _key(command.key)
    user = person(principal)
    pinned_now = toggle(user, kind, key)
    _audit(user, "Pinned." if pinned_now else "Unpinned.", kind=kind, key=key, pinned=pinned_now)
    return {"ok": True, "kind": kind, "key": key, "pinned": pinned_now}


@transaction.atomic
def move_pin(command: MoveCommand, *, principal: Principal) -> dict[str, Any]:
    """Move one of the person's pins past its neighbor: the ``pin.move`` capability."""

    kind, key = _kind(command.kind), _key(command.key)
    user = person(principal)
    move(user, kind, key, -1 if command.up else 1)
    _audit(user, "Moved a pin.", kind=kind, key=key, up=command.up)
    return {"ok": True, "kind": kind}


@transaction.atomic
def choose(command: ChooseCommand, *, principal: Principal) -> dict[str, Any]:
    """Set exactly which things of a kind the person pins: the ``pin.choose`` capability."""

    kind = _kind(command.kind)
    user = person(principal)
    replace(user, kind, command.keys)
    _audit(user, "Chose what to pin.", kind=kind, count=len(command.keys))
    return {"ok": True, "kind": kind}
