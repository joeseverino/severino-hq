"""What each connection provider's credential can see, as the last sweep found it.

Derived from the two registries: ``OBSERVATIONS`` names the readings a
provider feeds, and ``PROVIDERS`` names the resource kinds that list it in
``connection_providers``. A resource kind that declares ``unobserved_reason``
is read by no sweep and is left out. Each kind is joined to its
``ProviderInventory`` row, read once for the whole page.

A record that names its ``connection_ref`` belongs to that connection alone, so
two connections of one provider never show each other's readings.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hq.domains.control_plane.connection_kinds import CONNECTION_CREDENTIALS, CONNECTION_LABELS
from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
)
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.control_plane.reading_parts import PartRefusal, parts_of, refused_parts

from .labels import human_label
from .projection import read_once

READABLE = "readable"
# Read, with a declared part of it refused.
PARTIAL = "partial"
REFUSED = "refused"
UNREADABLE = "unreadable"
NOT_CONNECTED = "not_connected"
NEVER_SWEPT = "never_swept"

STATE_LABELS = {
    READABLE: "Readable",
    PARTIAL: "Partly read",
    REFUSED: "Refused",
    UNREADABLE: "Could not be read",
    NOT_CONNECTED: "Not connected",
    NEVER_SWEPT: "Never read",
}


@dataclass(frozen=True)
class Sight:
    """One reading or resource kind, and whether the credential can read it."""

    kind: str
    label: str
    # "reading" or "resource".
    source: str
    state: str
    records: int = 0
    observed_at: datetime | None = None
    error: str = ""
    requires: str = ""
    # One of REFUSALS when the provider refused the read.
    refusal: str = ""
    # The permissions ``requires`` names, one each.
    permissions: tuple[str, ...] = ()
    # The declared parts refused while the rest read.
    parts: tuple[PartRefusal, ...] = ()

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def missing(self) -> tuple[str, ...]:
        """The permissions to add: the kind's, or its refused parts'."""

        if self.state == REFUSED and self.refusal == PERMISSION_REFUSAL:
            return self.permissions
        return tuple(dict.fromkeys(name for part in self.parts for name in part.missing))

    @property
    def unseen(self) -> tuple[str, ...]:
        """What adding ``missing`` would let HQ see."""

        if not self.missing:
            return ()
        if self.state == PARTIAL:
            return tuple(dict.fromkeys(part.part.label for part in self.parts if part.missing))
        return (self.label,)

    @property
    def more_scope(self) -> str:
        """What the credential would also see, and with which permissions; else ""."""

        if not self.missing:
            return ""
        return f"Would also see {', '.join(self.unseen)} with {', '.join(self.missing)}"

    @property
    def remedy(self) -> str:
        if self.missing:
            return f"Add {', '.join(self.missing)} to see {', '.join(self.unseen)}"
        if self.parts:
            return "; ".join(self.part_reasons)
        return ""

    @property
    def part_reasons(self) -> tuple[str, ...]:
        """Why each refused part was not read, once each, where no permission opens it."""

        if self.missing:
            return ()
        return tuple(dict.fromkeys(part.phrase for part in self.parts))

    @property
    def listed(self) -> bool:
        """Whether the connection's list names it: not for a clean read of nothing."""

        return self.state != READABLE or self.records > 0


@dataclass(frozen=True)
class ProviderSight:
    provider: str
    label: str
    sights: tuple[Sight, ...]
    # Labels of the resource kinds this provider's credential acts on.
    manages: tuple[str, ...] = ()
    # Why the provider refused the credential itself, or "" when it did not.
    credential_refusal: str = ""

    def _count(self, state: str) -> int:
        return sum(sight.state == state for sight in self.sights)

    @property
    def tally(self) -> tuple[tuple[int, str], ...]:
        """(count, state label) for each state present, in STATE_LABELS order."""

        return tuple(
            (count, label)
            for state, label in STATE_LABELS.items()
            if (count := self._count(state))
        )

    @property
    def summary(self) -> str:
        """The tally as a sentence: "Reads 4 things", "Reads 1 thing, 1 partly read"."""

        from .ui import counted

        read = self._count(READABLE)
        parts = [f"Reads {counted(read, 'thing')}" if read else "Reads nothing"]
        parts.extend(
            f"{count} {label.lower()}"
            for state, label in STATE_LABELS.items()
            if state != READABLE and (count := self._count(state))
        )
        return ", ".join(parts)

    @property
    def read_at(self) -> datetime | None:
        """When what it reads was last read: the oldest of them, said once."""

        return min(
            (
                sight.observed_at
                for sight in self.sights
                if sight.state in (READABLE, PARTIAL) and sight.observed_at
            ),
            default=None,
        )

    @property
    def listed(self) -> tuple[Sight, ...]:
        """The sights the row names, see ``Sight.listed``."""

        return tuple(sight for sight in self.sights if sight.listed)

    @property
    def empty_count(self) -> int:
        """How many it reads cleanly and finds nothing in."""

        return len(self.sights) - len(self.listed)

    @property
    def sees(self) -> tuple[str, ...]:
        return tuple(sight.label for sight in self.sights)

    @property
    def refused_permission(self) -> tuple[Sight, ...]:
        """The readings, or parts of them, the credential is valid for and not
        permitted to read."""

        return tuple(sight for sight in self.sights if sight.missing)

    @property
    def missing(self) -> tuple[str, ...]:
        """Each permission a refused reading or part declares, once, sorted."""

        return tuple(
            sorted({name for sight in self.refused_permission for name in sight.missing})
        )

    @property
    def more_scope(self) -> tuple[str, ...]:
        """One ``Sight.more_scope`` line per reading a permission would open."""

        return tuple(sight.more_scope for sight in self.refused_permission)

    @property
    def unseen(self) -> tuple[str, ...]:
        """What those permissions would let HQ see."""

        return tuple(
            dict.fromkeys(label for sight in self.refused_permission for label in sight.unseen)
        )


def _through(row: ProviderInventory, connection_ref: str) -> list | None:
    """The row's records read through one connection; None when not attributable.

    A record naming a connection is that connection's. A kind whose records name
    none belongs to the provider as a whole.
    """

    records = [record for record in row.records or () if isinstance(record, dict)]
    if not connection_ref or not any("connection_ref" in record for record in records):
        return None
    return [record for record in records if record.get("connection_ref") == connection_ref]


def sight(
    kind: str,
    label: str,
    source: str,
    row: ProviderInventory | None,
    *,
    permissions: tuple[str, ...] = (),
    credential_valid: bool = False,
    records: int | None = None,
    parts: tuple[PartRefusal, ...] | None = None,
) -> Sight:
    """One kind's standing. ``credential_valid`` turns a credential refusal of
    this kind into a permission refusal: the same credential read something.
    ``parts`` are its refused parts, every stored one when not given."""

    requires = ", ".join(permissions)
    if row is None:
        return Sight(kind, label, source, NEVER_SWEPT, requires=requires, permissions=permissions)
    if not row.connected:
        return Sight(
            kind, label, source, NOT_CONNECTED, observed_at=row.observed_at,
            requires=requires, permissions=permissions,
        )
    if not row.reachable:
        refusal = row.refusal
        if refusal == CREDENTIAL_REFUSAL and credential_valid:
            refusal = PERMISSION_REFUSAL
        return Sight(
            kind,
            label,
            source,
            REFUSED if refusal else UNREADABLE,
            observed_at=row.observed_at,
            # A refused credential is said once for the provider, not per kind.
            error="" if refusal == CREDENTIAL_REFUSAL else row.error,
            requires=requires,
            refusal=refusal,
            permissions=permissions,
        )
    refused = refused_parts(row) if parts is None else parts
    return Sight(
        kind,
        label,
        source,
        PARTIAL if refused else READABLE,
        records=len(row.records or ()) if records is None else records,
        observed_at=row.observed_at,
        requires=requires,
        permissions=permissions,
        parts=refused,
    )


def standing(row: ProviderInventory, *, now: datetime | None = None) -> tuple[str, str]:
    """``(state, label)`` for one stored reading: a ``STATE_LABELS`` state, or
    freshness's ``STALE`` once a readable reading is out of date."""

    from .freshness import STALE, freshness

    found = sight(row.kind, "", "reading", row)
    if found.state in (READABLE, PARTIAL):
        aged = freshness(row.kind, row.observed_at, now)
        if aged.stale:
            return STALE, aged.label
    return found.state, found.state_label


def refused_outright(rows: Iterable[Any]) -> str:
    """The provider's words for refusing a credential outright, or "".

    Outright only when nothing it feeds was read: a credential that read one
    kind is valid, and its refusals elsewhere are missing permissions. ``rows``
    are one provider's inventory rows, with ``reachable``, ``refusal``, ``error``.
    """

    rows = tuple(rows)
    if any(row.reachable for row in rows):
        return ""
    return next(
        (
            row.error or "The service refused the credential."
            for row in rows
            if row.refusal == CREDENTIAL_REFUSAL
        ),
        "",
    )


def _inventory() -> dict[str, ProviderInventory]:
    return read_once("credential_inventory", _stored_inventory)


def _stored_inventory() -> dict[str, ProviderInventory]:
    from .facts import stored_snapshots

    return {row.kind: row for rows in stored_snapshots().values() for row in rows}


def _attributed(
    fed: tuple[str, str, str, tuple[str, ...]],
    row: ProviderInventory | None,
    connection_ref: str,
    credential_valid: bool,
    provider: str,
) -> Sight | None:
    """One kind's sight through one connection, or None when it read nothing there.

    Blank ``connection_ref`` is the provider as a whole. A ``PART_SOURCE`` item
    is only the parts of another provider's kind this credential reads.
    """

    kind, label, source, permissions = fed
    if row is not None:
        parts = _parts_for(row, provider, connection_ref, own=source != PART_SOURCE)
    standing = {"permissions": permissions, "credential_valid": credential_valid}
    if row is None or not row.connected or not row.reachable:
        return None if source == PART_SOURCE else sight(kind, label, source, row, **standing)
    through = None if source == PART_SOURCE else _through(row, connection_ref)
    if through is None:
        return sight(kind, label, source, row, parts=parts, **standing)
    if not through:
        return None
    return sight(kind, label, source, row, records=len(through), parts=parts, **standing)


# A fed item that is parts of another provider's kind: a zone's TLS posture,
# read through the account credential while the zone list is read through DNS.
PART_SOURCE = "part"


def _parts_for(
    row: ProviderInventory, provider: str, connection_ref: str, *, own: bool
) -> tuple[PartRefusal, ...]:
    """The row's refused parts this provider's credential read: its own kind's
    parts no other provider reads, or the parts it reads of another's."""

    return tuple(
        refused
        for refused in refused_parts(row)
        if (refused.part.provider in ("", provider) if own else refused.part.provider == provider)
        and not (connection_ref and refused.connection_ref not in ("", connection_ref))
    )


def credential_sight(
    connection_ref: str = "",
    *,
    inventory: dict[str, ProviderInventory] | None = None,
) -> tuple[ProviderSight, ...]:
    """Every credential-holding provider and what its credential can see.

    With ``connection_ref``, only what that connection read: a record naming
    another connection is not counted, and a kind with none of its records is
    left out.
    """

    inventory = _inventory() if inventory is None else inventory
    fed, manages = _fed()
    found = []
    for provider, kinds in sorted(fed.items()):
        refused = refused_outright(
            inventory[kind]
            for kind, _label, source, _permissions in kinds
            if kind in inventory and source != PART_SOURCE
        )
        sights = (
            _attributed(item, inventory.get(item[0]), connection_ref, not refused, provider)
            for item in kinds
        )
        found.append(
            ProviderSight(
                provider,
                CONNECTION_LABELS[provider],
                tuple(
                    sorted(
                        (seen for seen in sights if seen is not None),
                        key=lambda seen: (seen.source, seen.label),
                    )
                ),
                tuple(sorted(set(manages.get(provider, ())))),
                refused,
            )
        )
    return tuple(found)


def fed_kinds(provider: str) -> tuple[str, ...]:
    """Every stored kind ``provider``'s credential reads, whole or in part."""

    fed, _manages = _fed()
    return tuple(dict.fromkeys(kind for kind, *_rest in fed.get(provider, ())))


def _fed() -> tuple[
    dict[str, list[tuple[str, str, str, tuple[str, ...]]]], dict[str, list[str]]
]:
    """(kind, label, source, permissions) each provider's credential feeds, and
    the labels of the resource kinds it acts on."""

    fed: dict[str, list[tuple[str, str, str, tuple[str, ...]]]] = {
        provider: [] for provider in CONNECTION_CREDENTIALS
    }
    manages: dict[str, list[str]] = {}
    for kind, spec in OBSERVATIONS.items():
        if spec.provider in fed:
            fed[spec.provider].append((kind, spec.label, "reading", tuple(spec.requires)))
    for kind, spec in PROVIDERS.items():
        label = spec.label or human_label(kind)
        for provider in spec.connection_providers:
            manages.setdefault(provider, []).append(label)
            if not spec.unobserved_reason and provider in fed:
                fed[provider].append((kind, label, "resource", ()))
        _fed_parts(fed, kind, spec.connection_providers)
    return fed, manages


def _fed_parts(fed, kind: str, own: tuple[str, ...]) -> None:
    """The parts of ``kind`` another provider's credential reads, fed to it."""

    by_provider: dict[str, list] = {}
    for part in parts_of(kind).values():
        if part.provider and part.provider not in own and part.provider in fed:
            by_provider.setdefault(part.provider, []).append(part)
    for provider, parts in by_provider.items():
        fed[provider].append(
            (
                kind,
                ", ".join(part.label for part in parts),
                PART_SOURCE,
                tuple(dict.fromkeys(name for part in parts for name in part.requires)),
            )
        )


def sight_by_connection(
    connections: dict[str, str],
) -> tuple[dict[str, ProviderSight], tuple[ProviderSight, ...]]:
    """What each connection sees, by ref, and the providers with no connection.

    ``connections`` maps each connection ref to its provider. With nothing
    connected at all no controller has reported, so no provider is said to be
    missing one.
    """

    inventory = _inventory()
    by_ref = {
        connection_ref: found
        for connection_ref, provider in connections.items()
        for found in credential_sight(connection_ref, inventory=inventory)
        if found.provider == provider
    }
    connected = set(connections.values())
    return (
        by_ref,
        tuple(
            found
            for found in credential_sight(inventory=inventory)
            if found.provider not in connected
        )
        if connected
        else (),
    )
