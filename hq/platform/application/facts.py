"""The join engine: what HQ knows about a subject, and where from.

A subject is a set of hostnames, addresses and zones (a machine, a service
name, a domain). Readings join only through their spec's declared keys,
inventories through their provider's, tailnet devices and containers through
their own readers, so nothing else parses a record to find a join key. How a
page uses it is docs/Derived-Facts.md#facts-about-a-subject.
"""

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hq.domains.control_plane.names import certificate_covers, in_zone, normalized_hostname
from hq.domains.control_plane.observations import OBSERVATIONS, ObservationSpec
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.domains.control_plane.provider_spec import expiry_phrase
from hq.domains.control_plane.providers import PROVIDERS, read_record
from hq.domains.control_plane.reading_parts import PartRefusal, refused_parts
lazy from hq.domains.control_plane.models import ProviderInventory

from .derivations import passed
from .entity_links import kind_label
from .freshness import stale_after
from .locate import host_of
from .projection import read_once
from .timestamps import moment
from .ui import ended

_INVENTORY_KEY = "facts.inventory"
_READINGS_KEY = "facts.readings"


@dataclass(frozen=True, slots=True)
class Subject:
    """The join keys of whatever the facts are about, normalized once.

    ``zones`` names every hostname under each zone, the zone included;
    ``containers`` each container it is, by ``container_key``.
    """

    hostnames: frozenset[str]
    addresses: frozenset[str]
    zones: frozenset[str] = frozenset()
    containers: frozenset[str] = frozenset()

    @classmethod
    def of(
        cls,
        hostnames: Iterable[str] = (),
        addresses: Iterable[str] = (),
        zones: Iterable[str] = (),
        containers: Iterable[str] = (),
    ) -> Subject:
        return cls(
            containers=frozenset(key for key in containers if key),
            hostnames=frozenset(name for name in (normalized_hostname(str(h)) for h in hostnames) if name),
            addresses=frozenset(address for address in (host_of(a) for a in addresses) if address),
            zones=frozenset(zone for zone in (normalized_hostname(str(z)) for z in zones) if zone),
        )

    def __bool__(self) -> bool:
        return bool(self.hostnames or self.addresses or self.zones or self.containers)

    @property
    def dns_names(self) -> frozenset[str]:
        return frozenset(name for name in self.hostnames if "." in name)

    def matches(self, name: str) -> bool:
        """Whether one hostname key names this subject: exactly, by wildcard, or by zone."""

        key = normalized_hostname(str(name))
        if not key:
            return False
        if key in self.hostnames:
            return True
        if key.startswith("*.") and any(certificate_covers(hostname, frozenset({key})) for hostname in self.hostnames):
            return True
        return any(in_zone(key, zone) for zone in self.zones)

    def names(self, hostnames: Iterable[str]) -> bool:
        return any(self.matches(name) for name in hostnames)

    def holds(self, addresses: Iterable[str]) -> bool:
        return any(host_of(address) in self.addresses for address in addresses)


@dataclass(frozen=True, slots=True)
class Joined:
    """One reading record joined to a subject, and the keys that joined it."""

    spec: ObservationSpec
    record: Mapping[str, Any] = field(compare=False)
    connection_ref: str
    observed_at: datetime | None
    controller_id: str = ""
    hostnames: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()
    containers: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return self.spec.kind

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def facet(self) -> str:
        return self.spec.facet

    @property
    def by_address(self) -> bool:
        return bool(self.addresses) and not self.hostnames

    @property
    def by_container(self) -> bool:
        return bool(self.containers) and not self.hostnames and not self.addresses

    @property
    def relation(self) -> str:
        return self.spec.relation_to(by_address=self.by_address, by_container=self.by_container)

    @property
    def title(self) -> str:
        return self.spec.title(self.record)

    @property
    def expires(self) -> str:
        return self.spec.expires(self.record)

    @property
    def issuer(self) -> str:
        return self.spec.issuer(self.record)

    @property
    def expiry(self) -> str:
        """The expiry as a person reads it: the date and the days left."""

        return expiry_phrase(self.expires) if self.expires else ""

    @property
    def stale(self) -> bool:
        if self.observed_at is None:
            return False
        return passed(self.observed_at + stale_after(self.kind))

    @property
    def unread(self) -> str:
        """Why part of the record could not be read, as one line."""

        unread = self.record.get("unread")
        if isinstance(unread, Mapping):
            return "; ".join(f"{part}: {reason}" for part, reason in unread.items())
        return str(unread or "")


@dataclass(frozen=True, slots=True)
class Unread:
    """A reading kind whose last read was refused, and why."""

    spec: ObservationSpec
    error: str

    @property
    def detail(self) -> str:
        because = self.error or "The last read failed and gave no reason."
        requires = ", ".join(self.spec.requires)
        return ended(f"{because} Requires: {requires}") if requires else because


@dataclass(frozen=True, slots=True)
class _Entry:
    spec: ObservationSpec
    snapshot: Any
    record: Mapping[str, Any]
    connection_ref: str
    hostnames: tuple[str, ...]
    addresses: tuple[str, ...]
    containers: tuple[str, ...] = ()


class Readings:
    """Every stored reading record, indexed by its declared join keys."""

    def __init__(
        self,
        entries: Iterable[_Entry],
        unread: Iterable[Unread],
        fronted: Callable[[str], frozenset[str]] = lambda _kind: frozenset(),
    ):
        self._entries = tuple(entries)
        self._unread = tuple(unread)
        self._fronted = fronted
        self._fronted_by: dict[str, frozenset[str]] = {}
        self._by_name: dict[str, list[int]] = {}
        self._by_address: dict[str, list[int]] = {}
        self._by_container: dict[str, list[int]] = {}
        for index, entry in enumerate(self._entries):
            for name in entry.hostnames:
                self._by_name.setdefault(name, []).append(index)
            for address in entry.addresses:
                self._by_address.setdefault(address, []).append(index)
            for container in entry.containers:
                self._by_container.setdefault(container, []).append(index)

    def about(
        self,
        subject: Subject,
        *,
        kinds: Iterable[str] | None = None,
        facets: Iterable[str] | None = None,
    ) -> tuple[Joined, ...]:
        """The records joined to ``subject``, in registry and record order."""

        wanted = _Wanted.of(kinds, facets)
        entries = (self._entries[index] for index in sorted(self._indexes_about(subject)))
        joined = (self._joined(entry, subject) for entry in entries if wanted.admits(entry.spec))
        return tuple(item for item in joined if item is not None)

    def _indexes_about(self, subject: Subject) -> set[int]:
        """The entries any of ``subject``'s keys reach through the indexes: a
        hostname or the wildcard over it, a name in one of its zones, an
        address, a container."""

        found: set[int] = set()
        for hostname in subject.hostnames:
            found.update(self._by_name.get(hostname, ()))
            parent = hostname.partition(".")[2]
            if parent:
                found.update(self._by_name.get(f"*.{parent}", ()))
        if subject.zones:
            for name, indexes in self._by_name.items():
                if any(in_zone(name, zone) for zone in subject.zones):
                    found.update(indexes)
        for address in subject.addresses:
            found.update(self._by_address.get(address, ()))
        for container in subject.containers:
            found.update(self._by_container.get(container, ()))
        return found

    def _joined(self, entry: _Entry, subject: Subject) -> Joined | None:
        """``entry`` joined to ``subject`` by the keys that name it, or None when none does."""

        # Only the keys that name this subject: a resolver list names
        # other machines too.
        names = tuple(name for name in entry.hostnames if subject.matches(name))
        if entry.spec.fronted_by and names:
            names = self._fronted_names(entry, names, subject)
        addresses = tuple(address for address in entry.addresses if address in subject.addresses)
        containers = tuple(key for key in entry.containers if key in subject.containers)
        if not (names or addresses or containers):
            return None
        return Joined(
            spec=entry.spec,
            record=entry.record,
            connection_ref=entry.connection_ref,
            observed_at=record_read_at(entry.record) or entry.snapshot.observed_at,
            controller_id=entry.snapshot.controller_id,
            hostnames=names,
            addresses=addresses,
            containers=containers,
        )

    def hostnames(self, *, names_services: bool = False) -> tuple[str, ...]:
        """Every hostname key the stored records hold; with ``names_services``,
        only those of kinds whose names are services."""

        return tuple(
            dict.fromkeys(
                name
                for entry in self._entries
                if entry.spec.names_services or not names_services
                for name in entry.hostnames
            )
        )

    def _fronted_names(self, entry: _Entry, names: tuple[str, ...], subject: Subject) -> tuple[str, ...]:
        """The keys that apply: under a subject zone, or naming a subject
        hostname that a record of ``fronted_by`` fronts."""

        kind = entry.spec.fronted_by
        if kind not in self._fronted_by:
            self._fronted_by[kind] = self._fronted(kind)
        fronted = self._fronted_by[kind]
        served = frozenset(name for name in subject.hostnames if name in fronted)
        return tuple(
            name
            for name in names
            if any(in_zone(name, zone) for zone in subject.zones)
            or any(certificate_covers(hostname, frozenset({name})) for hostname in served)
        )

    def unread(
        self,
        *,
        kinds: Iterable[str] | None = None,
        facets: Iterable[str] | None = None,
    ) -> tuple[Unread, ...]:
        """Kinds whose last read was refused. Not connected kinds are not here."""

        wanted = _Wanted.of(kinds, facets)
        return tuple(item for item in self._unread if wanted.admits(item.spec))


@dataclass(frozen=True, slots=True)
class _Wanted:
    """The kinds and facets a caller narrowed a reading to; None admits every one."""

    kinds: frozenset[str] | None
    facets: frozenset[str] | None

    @classmethod
    def of(cls, kinds: Iterable[str] | None, facets: Iterable[str] | None) -> _Wanted:
        return cls(
            frozenset(kinds) if kinds is not None else None,
            frozenset(facets) if facets is not None else None,
        )

    def admits(self, spec: ObservationSpec) -> bool:
        return (self.kinds is None or spec.kind in self.kinds) and (self.facets is None or spec.facet in self.facets)


def record_read_at(record: Mapping[str, Any]) -> datetime | None:
    """When a record says it was read, for a reading carried across sweeps."""

    return moment(record.get("read_at"), naive="keep")


def readings() -> Readings:
    """Every connected reading kind, indexed once per projection."""

    def build() -> Readings:
        entries: list[_Entry] = []
        unread: list[Unread] = []
        for spec in OBSERVATIONS.values():
            for snapshot in _inventory(spec.kind):
                if not snapshot.reachable:
                    unread.append(Unread(spec, snapshot.error))
                    continue
                # Stored records are already schema-filtered at ingest.
                entries.extend(
                    _Entry(
                        spec=spec,
                        snapshot=snapshot,
                        record=_frozen(record),
                        connection_ref=str(record.get("connection_ref", "") or ""),
                        hostnames=tuple(
                            dict.fromkeys(
                                name
                                for name in (normalized_hostname(str(item)) for item in spec.hostnames(record))
                                if name
                            )
                        ),
                        addresses=tuple(
                            dict.fromkeys(
                                address for address in (host_of(item) for item in spec.addresses(record)) if address
                            )
                        ),
                        containers=tuple(dict.fromkeys(spec.containers(record))),
                    )
                    for record in snapshot.records
                )
        return Readings(entries, unread, fronted_names)

    return read_once(_READINGS_KEY, build)


def fronted_names(kind: str) -> frozenset[str]:
    """Hostnames a swept record of ``kind`` puts its provider in front of.

    Read through the provider's ``fronts``; a kind with none fronts nothing.
    """

    provider = PROVIDERS.get(kind)
    if provider is None or provider.fronts is None or provider.from_record is None:
        return frozenset()

    def fronted(spec: dict[str, Any]) -> tuple[str, ...]:
        if not provider.fronts(spec) or not provider.hostnames:
            return ()
        return tuple(provider.hostnames(spec))

    def build() -> frozenset[str]:
        found: set[str] = set()
        for _snapshot, record in inventory_records(kind):
            names = read_record(kind, record, fronted) or ()
            found.update(name for name in (normalized_hostname(str(n)) for n in names) if name)
        return frozenset(found)

    return read_once(f"facts.fronted:{kind}", build)


def reading_could_join(spec: ObservationSpec, subject: Subject) -> bool:
    """Whether a record of ``spec`` could say anything about ``subject``.

    A reading names DNS names, so a subject's bare machine name (no dot) is not
    one it could join.
    """

    return bool(
        (spec.joins_hostnames and (subject.dns_names or subject.zones)) or (spec.joins_addresses and subject.addresses)
    )


def inventory_could_join(kind: str, subject: Subject) -> bool:
    """Whether a record of resource ``kind`` could say anything about ``subject``."""

    if kind == CONTAINER_KIND:
        return bool(subject.addresses)
    provider = PROVIDERS.get(kind)
    if provider is None or provider.from_record is None:
        return False
    by_name = provider.hostnames is not None or provider.identity is not None
    by_address = provider.answers is not None or provider.origin is not None
    return bool(
        (by_name and (subject.hostnames or subject.zones)) or (by_address and (subject.addresses or subject.hostnames))
    )


def part_refusals(kinds: Iterable[str] | None = None) -> tuple[PartRefusal, ...]:
    """Every part refused on a connected snapshot that otherwise read."""

    def load() -> tuple[PartRefusal, ...]:
        return tuple(
            refused
            for rows in _stored().values()
            for snapshot in rows
            if snapshot.connected and snapshot.reachable
            for refused in refused_parts(snapshot)
        )

    wanted = frozenset(kinds) if kinds is not None else None
    return tuple(
        refused for refused in read_once("facts.part_refusals", load) if wanted is None or refused.kind in wanted
    )


def refusals_about(subject: Subject, kinds: Iterable[str] | None = None) -> tuple[PartRefusal, ...]:
    """The refused parts that could hide something about ``subject``."""

    return tuple(refused for refused in part_refusals(kinds) if _refusal_about(refused, subject))


def _refusal_about(refused: PartRefusal, subject: Subject) -> bool:
    if not refused.scope:
        spec = OBSERVATIONS.get(refused.kind)
        if spec is not None:
            return reading_could_join(spec, subject)
        return bool(inventory_about(refused.kind, subject))
    return (
        refused.holds(subject.addresses)
        or any(refused.covers(name) for name in subject.hostnames)
        or any(in_zone(refused.scope, zone) or in_zone(zone, refused.scope) for zone in subject.zones)
    )


def unreadable_labels(subject: Subject | None = None) -> tuple[str, ...]:
    """The labels of joined kinds whose last read was refused, and of refused
    parts.

    With ``subject``, only kinds that could say something about it: a refused
    kind that joins nothing a page is about cannot change that page.
    """

    labels = {
        item.spec.label for item in readings().unread() if subject is None or reading_could_join(item.spec, subject)
    }
    labels.update(refused.part.label for refused in (part_refusals() if subject is None else refusals_about(subject)))
    for kind, provider in PROVIDERS.items():
        if provider.from_record is None and kind != CONTAINER_KIND:
            continue
        if subject is not None and not inventory_could_join(kind, subject):
            continue
        if any(not snapshot.reachable for snapshot in _inventory(kind)):
            labels.add(kind_label(kind))
    return tuple(sorted(labels))


def inventory_records(kind: str) -> tuple[tuple[Any, Mapping[str, Any]], ...]:
    """``(snapshot, record)`` for every record of a connected, reachable kind."""

    return tuple(
        (snapshot, record) for snapshot in _inventory(kind) if snapshot.reachable for record in snapshot.records
    )


def inventory_about(kind: str, subject: Subject) -> tuple[tuple[Any, Mapping[str, Any]], ...]:
    """``(snapshot, record)`` for each record of one resource kind joined to ``subject``.

    Joined through the provider's ``hostnames``, ``answers``, ``origin``, or an
    ``identity`` of one name. Unreachable snapshots are left out; a record the
    provider cannot read is skipped.
    """

    provider = PROVIDERS.get(kind)
    if provider is None or provider.from_record is None:
        return ()
    found = []
    for snapshot, record in inventory_records(kind):
        keys = read_record(kind, record, lambda spec: _spec_keys(provider, spec, by_identity=True))
        if keys is not None and _keys_about(subject, *keys):
            found.append((snapshot, record))
    return tuple(found)


def _joined_kinds() -> tuple[str, ...]:
    """The kinds read here other than the tailnet, which has its own reader."""

    return (
        tuple(OBSERVATIONS)
        + tuple(kind for kind, provider in PROVIDERS.items() if provider.from_record is not None)
        + (CONTAINER_KIND,)
    )


def connection_facts(connection_ref: str, provider: str) -> tuple[tuple[str, str], ...]:
    """What the readings this connection took say about it, through ``ObservationSpec.facts``.

    A record naming no connection speaks for every connection of its provider.
    """

    found: list[tuple[str, str]] = []
    for spec in OBSERVATIONS.values():
        if spec.provider != provider:
            continue
        for record in _records_for_connection(spec.kind, connection_ref):
            found.extend(fact for fact in spec.facts(record) if fact not in found)
    return tuple(found)


def _records_for_connection(kind: str, connection_ref: str) -> Iterator[Mapping[str, Any]]:
    """The records of one kind's readable snapshots that speak for ``connection_ref``:
    its own, and those naming no connection."""

    for snapshot in _inventory(kind):
        if not snapshot.reachable:
            continue
        for record in snapshot.records:
            ref = str(record.get("connection_ref", "") or "")
            if not ref or ref == connection_ref:
                yield record


def snapshots_of(kind: str) -> tuple[Any, ...]:
    """Connected snapshots of one kind the engine reads, from its one read."""

    return _inventory(kind)


def stored_snapshots() -> Mapping[str, tuple[Any, ...]]:
    """Every stored snapshot by kind, connected or not, from the same read."""

    return _stored()


def _stored() -> dict[str, tuple[Any, ...]]:
    """One read of every stored snapshot, shared by the joins and credential sight."""

    def load() -> dict[str, tuple[Any, ...]]:
        grouped: dict[str, list[Any]] = {}
        for snapshot in ProviderInventory.objects.all():
            grouped.setdefault(snapshot.kind, []).append(snapshot)
        return {name: tuple(rows) for name, rows in grouped.items()}

    return read_once(_INVENTORY_KEY, load)


def _inventory(kind: str) -> tuple[Any, ...]:
    """Connected snapshots of one kind the engine joins, from the one read."""

    if kind not in _joined_kinds():
        return ()
    return tuple(snapshot for snapshot in _stored().get(kind, ()) if snapshot.connected)


def _frozen(record: Mapping[str, Any]) -> Mapping[str, Any]:
    return frozendict(dict(record))


def _spec_keys(provider, spec, *, by_identity: bool = False) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """The hostnames, answers and origin a provider reads out of one record's spec.

    With ``by_identity``, a record naming no hostname is named by its identity
    when that is one name.
    """

    names = tuple(provider.hostnames(spec)) if provider.hostnames else ()
    if by_identity and not names and provider.identity is not None:
        identity = tuple(provider.identity(spec))
        names = identity if len(identity) == 1 else ()
    answers = tuple(provider.answers(spec)) if provider.answers else ()
    origin = provider.origin(spec) if provider.origin else ""
    return names, answers, origin


def _keys_about(subject: Subject, names: tuple[str, ...], answers: tuple[str, ...], origin: str) -> bool:
    """Whether a record's keys reach ``subject``: a name it has, an address it
    holds, or an origin at either."""

    return bool(
        subject.names(names)
        or subject.holds(answers)
        or (origin and (subject.holds((origin,)) or subject.names((host_of(origin),))))
    )
