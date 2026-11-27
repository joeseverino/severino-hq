"""What each connection provider's credential can see, as the last sweep found it.

Derived from ``providers.provider_reads``, the one index of what reads what; the
states a kind can be in are docs/Derived-Facts.md#what-a-credential-can-see. A
record that names its ``connection_ref`` belongs to that connection alone, so
two connections of one provider never show each other's readings.
"""

from dataclasses import dataclass
from datetime import datetime

from hq.domains.control_plane.connection_kinds import CONNECTION_CREDENTIALS, CONNECTION_LABELS
from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
)
from hq.domains.control_plane.providers import PROVIDERS, Read, provider_reads
from hq.domains.control_plane.reading_parts import RefusedPart, refused_parts

from . import readings
from .projection import read_once
from .tone import Tone, TonedChoices
lazy from .facts import records_through, stored_snapshots
lazy from .freshness import STALE, Freshness, freshness
lazy from .ui import counted


class SightState(TonedChoices):
    """How a kind's last read went, in the tone its word is drawn in beside
    the kind; one nothing tried (never read, not connected) is said quietly."""

    READABLE = "readable", Tone.GOOD, "Readable"
    # Read, with a declared part of it refused.
    PARTIAL = "partial", Tone.ATTENTION, "Partly read"
    REFUSED = "refused", Tone.SERIOUS, "Refused"
    UNREADABLE = "unreadable", Tone.SERIOUS, "Could not be read"
    NOT_CONNECTED = "not_connected", Tone.NEUTRAL, "Not connected"
    NEVER_SWEPT = "never_swept", Tone.NEUTRAL, "Never read"


READABLE, PARTIAL, REFUSED, UNREADABLE, NOT_CONNECTED, NEVER_SWEPT = SightState


@dataclass(frozen=True, slots=True)
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
    parts: tuple[RefusedPart, ...] = ()

    @property
    def state_label(self) -> str:
        return SightState(self.state).label

    @property
    def tone(self) -> Tone:
        return SightState(self.state).tone

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
        """Why the refused parts were not read, where no permission opens them:
        one line per reason, naming every part and everything it was refused
        on ("Code scanning alerts, Branch rules not read on app, site: ...")."""

        if self.missing:
            return ()
        grouped: dict[str, tuple[dict[str, None], dict[str, None]]] = {}
        for part in self.parts:
            labels, scopes = grouped.setdefault(part.said, ({}, {}))
            labels[part.part.label] = None
            if part.scope:
                scopes[part.scope.rsplit("/", 1)[-1]] = None
        return tuple(
            f"{', '.join(labels)} not read{' on ' + ', '.join(scopes) if scopes else ''}: {reason}"
            for reason, (labels, scopes) in grouped.items()
        )

    @property
    def listed(self) -> bool:
        """Whether the connection's list names it: not for a clean read of nothing."""

        return self.state != READABLE or self.records > 0


@dataclass(frozen=True, slots=True)
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
        """(count, state label) for each state present, in ``SightState``'s order."""

        return tuple((count, state.label) for state in SightState if (count := self._count(state)))

    @property
    def summary(self) -> str:
        """The tally as a sentence: "Reads 4 things", "Reads 1 thing, 1 partly read"."""

        read = self._count(READABLE)
        parts = [f"Reads {counted(read, 'thing')}" if read else "Reads nothing"]
        parts.extend(
            f"{count} {state.label.lower()}"
            for state in SightState
            if state != READABLE and (count := self._count(state))
        )
        return ", ".join(parts)

    @property
    def age(self) -> Freshness | None:
        """How old what it reads is, said once: an out-of-date reading's age,
        else the oldest's; None when nothing was read."""

        read = (found for found in map(Standing.of, self.sights) if found.read and found.age.observed_at)
        return min(
            (found.age for found in read),
            key=lambda age: (not age.stale, age.observed_at),
            default=None,
        )

    @property
    def listed(self) -> tuple[Sight, ...]:
        """The sights the row names, see ``Sight.listed``."""

        return tuple(sight for sight in self.sights if sight.listed)

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

        return tuple(sorted({name for sight in self.refused_permission for name in sight.missing}))

    @property
    def more_scope(self) -> tuple[str, ...]:
        """One ``Sight.more_scope`` line per reading a permission would open."""

        return tuple(sight.more_scope for sight in self.refused_permission)

    @property
    def unseen(self) -> tuple[str, ...]:
        """What those permissions would let HQ see."""

        return tuple(dict.fromkeys(label for sight in self.refused_permission for label in sight.unseen))


def sight(
    kind: str,
    label: str,
    source: str,
    row: ProviderInventory | None,
    *,
    permissions: tuple[str, ...] = (),
    credential_valid: bool = False,
    records: int | None = None,
    parts: tuple[RefusedPart, ...] | None = None,
) -> Sight:
    """One kind's standing. ``credential_valid`` turns a credential refusal of
    this kind into a permission refusal: the same credential read something.
    ``parts`` are its refused parts, every stored one when not given."""

    requires = ", ".join(permissions)
    if row is None:
        return Sight(kind, label, source, NEVER_SWEPT, requires=requires, permissions=permissions)
    if not row.connected:
        return Sight(
            kind,
            label,
            source,
            NOT_CONNECTED,
            observed_at=row.observed_at,
            requires=requires,
            permissions=permissions,
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


@dataclass(frozen=True, slots=True)
class Standing:
    """A kind's last read and its age: the one state every surface names it by,
    a ``SightState`` or freshness's ``STALE`` once what was read is out of date."""

    sight: Sight
    age: Freshness

    @classmethod
    def of(cls, seen: Sight, now: datetime | None = None) -> Standing:
        return cls(seen, freshness(seen.kind, seen.observed_at, now))

    @property
    def read(self) -> bool:
        return self.sight.state in (READABLE, PARTIAL)

    @property
    def failed(self) -> bool:
        return self.sight.state in (REFUSED, UNREADABLE)

    @property
    def stale(self) -> bool:
        # Only a reading that was taken can be out of date; a refused one says so.
        return self.read and self.age.stale

    @property
    def state(self) -> str:
        return STALE if self.stale else self.sight.state

    @property
    def label(self) -> str:
        return self.age.label if self.stale else self.sight.state_label

    @property
    def tone(self) -> Tone:
        return Tone.ATTENTION if self.stale else self.sight.tone


def standing(
    kind: str,
    row: ProviderInventory | None,
    *,
    label: str = "",
    permissions: tuple[str, ...] = (),
    now: datetime | None = None,
) -> Standing:
    """One stored kind's standing, every surface's. A credential refused for
    it that read something else is missing a permission, as the credential's
    own sight says (``credential_sight``)."""

    refused = row is not None and not row.reachable and row.refusal == CREDENTIAL_REFUSAL
    found = sight(
        kind, label, "reading", row, permissions=permissions, credential_valid=refused and _credential_valid(kind)
    )
    return Standing.of(found, now)


def _credential_valid(kind: str) -> bool:
    inventory = _inventory()
    return any(kind in fed_kinds(provider) and not _refused(provider, inventory) for provider in CONNECTION_CREDENTIALS)


def _refused(provider: str, inventory: dict[str, ProviderInventory]) -> str:
    """The provider's words for refusing its credential outright, or "".

    Outright only when nothing it feeds was read: a credential that read one
    kind is valid, and its refusals elsewhere are missing permissions.
    """

    rows = tuple(inventory[read.kind] for read in _fed(provider) if read.kind in inventory and read.source != "part")
    if any(row.reachable for row in rows):
        return ""
    return next(
        (row.error or "The service refused the credential." for row in rows if row.refusal == CREDENTIAL_REFUSAL),
        "",
    )


def _inventory() -> dict[str, ProviderInventory]:
    return read_once("credential_inventory", _stored_inventory)


def _stored_inventory() -> dict[str, ProviderInventory]:
    return {row.kind: row for rows in stored_snapshots().values() for row in rows}


def _attributed(
    read: Read,
    row: ProviderInventory | None,
    connection_ref: str,
    *,
    credential_valid: bool,
    provider: str,
) -> Sight | None:
    """One kind's sight through one connection, or None when it read nothing there.

    Blank ``connection_ref`` is the provider as a whole. A ``part`` read is
    only the parts of another provider's kind this credential reads.
    """

    part = read.source == "part"
    if row is not None:
        parts = _parts_for(row, provider, connection_ref, own=not part)
    standing = {"permissions": read.permissions, "credential_valid": credential_valid}
    if row is None or not row.connected or not row.reachable:
        return None if part else sight(read.kind, read.label, read.source, row, **standing)
    if part:
        return sight(read.kind, read.label, read.source, row, parts=parts, **standing)
    through = records_through(row.records, connection_ref)
    if row.records and not through:
        return None
    return sight(read.kind, read.label, read.source, row, records=len(through), parts=parts, **standing)


def _parts_for(row: ProviderInventory, provider: str, connection_ref: str, *, own: bool) -> tuple[RefusedPart, ...]:
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
    found = []
    for provider in sorted(CONNECTION_CREDENTIALS):
        refused = _refused(provider, inventory)
        sights = (
            _attributed(read, inventory.get(read.kind), connection_ref, credential_valid=not refused, provider=provider)
            for read in _fed(provider)
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
                tuple(sorted({spec.label for spec in PROVIDERS.values() if provider in spec.connection_providers})),
                refused,
            )
        )
    return tuple(found)


def fed_kinds(provider: str) -> tuple[str, ...]:
    """Every stored kind ``provider``'s credential reads, whole or in part."""

    return tuple(dict.fromkeys(read.kind for read in _fed(provider)))


def _fed(provider: str) -> tuple[Read, ...]:
    """What a credential-holding provider's credential reads from the inventory."""

    if provider not in CONNECTION_CREDENTIALS:
        return ()
    return tuple(read for read in provider_reads().get(provider, ()) if read.kept is None)


def read_through(provider: str, label: str) -> ProviderSight | None:
    """What came through a connection HQ reaches itself, with no credential:
    each reading HQ takes through it, as stored. None when none does."""

    def load() -> ProviderSight | None:
        inventory = _inventory()
        sights = [
            _kept(read.kept, read.kept.through[provider])
            if read.kept is not None
            else sight(read.kind, read.label, "reading", inventory.get(read.kind))
            for read in provider_reads().get(provider, ())
            if read.by == "hq"
        ]
        return ProviderSight(provider, label, tuple(sorted(sights, key=lambda seen: seen.label))) if sights else None

    return read_once(f"credential_sight.through:{provider}", load)


def _kept(kept: readings.Kept, answered) -> Sight:
    """One kind of kept reading through one connection: read when any stored
    value says the connection answered, and as of the newest that does."""

    stored = kept.stored()
    good = [item.observed_at for item in stored if answered(item.value)]
    if good:
        return Sight(kept.cadence, kept.label, "reading", READABLE, records=len(good), observed_at=max(good))
    if stored:
        return Sight(
            kept.cadence, kept.label, "reading", UNREADABLE, observed_at=max(item.observed_at for item in stored)
        )
    return Sight(kept.cadence, kept.label, "reading", NEVER_SWEPT)


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
        tuple(found for found in credential_sight(inventory=inventory) if found.provider not in connected)
        if connected
        else (),
    )
