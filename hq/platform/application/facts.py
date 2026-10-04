"""The join engine: what HQ knows about a subject, and where from.

A subject is a set of hostnames, addresses and zones: a machine, a service name,
a domain (every name under it). Readings join only through their spec's declared
``hostnames`` and ``addresses``; resource inventories join through the
provider's ``hostnames``, ``answers``, ``origin`` or a single-name ``identity``;
tailnet devices and containers through their own readers. Nothing else parses a
record to find a join key.

``readings()`` indexes every stored reading record by its keys, once per
projection; ``Readings.about(subject)`` returns the records joined to one
subject. ``facts_about`` projects those and the inventories into facts.

A kind stored as unreachable is one unreadable fact carrying its error and
``requires``. A kind no connection could read (``connected`` false) yields
nothing: it is not a failed read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType, SimpleNamespace
from typing import Any, Callable, Iterable, Iterator, Mapping

from django.utils import timezone

from hq.domains.control_plane.names import in_zone, certificate_covers, normalized_hostname
from hq.domains.control_plane.observations import OBSERVATIONS, ObservationSpec
from hq.domains.control_plane.reading_parts import PartRefusal, refused_parts
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.domains.control_plane.provider_spec import expiry_phrase

from .entity_links import kind_label
from .freshness import stale_after
from .locate import host_of
from .projection import read_once
from .tailnet import TAILNET_KIND
from .timestamps import moment

OBSERVED = "observed"
UNREADABLE = "unreadable"
STALE = "stale"

_INVENTORY_KEY = "facts.inventory"
_READINGS_KEY = "facts.readings"


@dataclass(frozen=True)
class Fact:
    """One thing a source says about a subject."""

    label: str
    value: str
    source_kind: str
    source_label: str
    connection_ref: str = ""
    observed_at: datetime | None = None
    state: str = OBSERVED
    # Context for an observed fact; the reason and ``requires`` for an
    # unreadable one.
    detail: str = ""
    # The schema-filtered reading the fact came from, for the raw readout. Only
    # readings carry one: an inventory record is not filtered by a schema.
    record: Mapping[str, Any] | None = field(default=None, compare=False)

    @property
    def source(self) -> tuple[str, str]:
        return (self.source_kind, self.connection_ref)


@dataclass(frozen=True)
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
    ) -> "Subject":
        return cls(
            containers=frozenset(key for key in containers if key),
            hostnames=frozenset(
                name for name in (normalized_hostname(str(h)) for h in hostnames) if name
            ),
            addresses=frozenset(
                address for address in (host_of(a) for a in addresses) if address
            ),
            zones=frozenset(
                zone for zone in (normalized_hostname(str(z)) for z in zones) if zone
            ),
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
        if key.startswith("*.") and any(
            certificate_covers(hostname, frozenset({key})) for hostname in self.hostnames
        ):
            return True
        return any(in_zone(key, zone) for zone in self.zones)

    def names(self, hostnames: Iterable[str]) -> bool:
        return any(self.matches(name) for name in hostnames)

    def holds(self, addresses: Iterable[str]) -> bool:
        return any(host_of(address) in self.addresses for address in addresses)


@dataclass(frozen=True)
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
        return self.observed_at < timezone.now() - stale_after(self.kind)

    @property
    def unread(self) -> str:
        """Why part of the record could not be read, as one line."""

        unread = self.record.get("unread")
        if isinstance(unread, Mapping):
            return "; ".join(f"{part}: {reason}" for part, reason in unread.items())
        return str(unread or "")


@dataclass(frozen=True)
class Unread:
    """A reading kind whose last read was refused, and why."""

    spec: ObservationSpec
    error: str
    observed_at: datetime | None

    @property
    def detail(self) -> str:
        because = self.error or "The last read failed and gave no reason."
        requires = ", ".join(self.spec.requires)
        return f"{because} Requires: {requires}." if requires else because


@dataclass(frozen=True)
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

        wanted_kinds = frozenset(kinds) if kinds is not None else None
        wanted_facets = frozenset(facets) if facets is not None else None
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
        joined = []
        for index in sorted(found):
            entry = self._entries[index]
            if wanted_kinds is not None and entry.spec.kind not in wanted_kinds:
                continue
            if wanted_facets is not None and entry.spec.facet not in wanted_facets:
                continue
            # Only the keys that name this subject: a resolver list names
            # other machines too.
            names = tuple(name for name in entry.hostnames if subject.matches(name))
            if entry.spec.fronted_by and names:
                names = self._fronted_names(entry, names, subject)
            addresses = tuple(
                address for address in entry.addresses if address in subject.addresses
            )
            containers = tuple(key for key in entry.containers if key in subject.containers)
            if not (names or addresses or containers):
                continue
            joined.append(
                Joined(
                    spec=entry.spec,
                    record=entry.record,
                    connection_ref=entry.connection_ref,
                    observed_at=record_read_at(entry.record) or entry.snapshot.observed_at,
                    controller_id=str(getattr(entry.snapshot, "controller_id", "") or ""),
                    hostnames=names,
                    addresses=addresses,
                    containers=containers,
                )
            )
        return tuple(joined)

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

    def _fronted_names(
        self, entry: _Entry, names: tuple[str, ...], subject: Subject
    ) -> tuple[str, ...]:
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

        wanted_kinds = frozenset(kinds) if kinds is not None else None
        wanted_facets = frozenset(facets) if facets is not None else None
        return tuple(
            item
            for item in self._unread
            if (wanted_kinds is None or item.spec.kind in wanted_kinds)
            and (wanted_facets is None or item.spec.facet in wanted_facets)
        )


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
                    unread.append(Unread(spec, snapshot.error, snapshot.observed_at))
                    continue
                # Stored records are already schema-filtered at ingest.
                for record in snapshot.records:
                    entries.append(
                        _Entry(
                            spec=spec,
                            snapshot=snapshot,
                            record=_frozen(record),
                            connection_ref=str(record.get("connection_ref", "") or ""),
                            hostnames=tuple(
                                dict.fromkeys(
                                    name
                                    for name in (
                                        normalized_hostname(str(item))
                                        for item in spec.hostnames(record)
                                    )
                                    if name
                                )
                            ),
                            addresses=tuple(
                                dict.fromkeys(
                                    address
                                    for address in (
                                        host_of(item) for item in spec.addresses(record)
                                    )
                                    if address
                                )
                            ),
                            containers=tuple(dict.fromkeys(spec.containers(record))),
                        )
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

    def build() -> frozenset[str]:
        found: set[str] = set()
        for _snapshot, record in inventory_records(kind):
            try:
                spec = provider.from_record(record)
                if not provider.fronts(spec):
                    continue
                names = provider.hostnames(spec) if provider.hostnames else ()
            except (KeyError, TypeError, ValueError):
                continue
            found.update(
                name for name in (normalized_hostname(str(n)) for n in names) if name
            )
        return frozenset(found)

    return read_once(f"facts.fronted:{kind}", build)


def reading_could_join(spec: ObservationSpec, subject: Subject) -> bool:
    """Whether a record of ``spec`` could say anything about ``subject``.

    A reading names DNS names, so a subject's bare machine name (no dot) is not
    one it could join.
    """

    return bool(
        (spec.joins_hostnames and (subject.dns_names or subject.zones))
        or (spec.joins_addresses and subject.addresses)
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
        (by_name and (subject.hostnames or subject.zones))
        or (by_address and (subject.addresses or subject.hostnames))
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
        refused
        for refused in read_once("facts.part_refusals", load)
        if wanted is None or refused.kind in wanted
    )


def refusals_about(
    subject: Subject, kinds: Iterable[str] | None = None
) -> tuple[PartRefusal, ...]:
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
        or any(
            in_zone(refused.scope, zone) or in_zone(zone, refused.scope)
            for zone in subject.zones
        )
    )


def _part_fact(refused: PartRefusal) -> Fact:
    return Fact(
        label=refused.part.label,
        value="",
        source_kind=refused.kind,
        source_label=kind_label(refused.kind),
        connection_ref=refused.connection_ref,
        state=UNREADABLE,
        detail=refused.phrase,
    )


def _part_facts(subject: Subject) -> Iterator[Fact]:
    for refused in refusals_about(subject):
        yield _part_fact(refused)


def unreadable_labels(subject: Subject | None = None) -> tuple[str, ...]:
    """The labels of joined kinds whose last read was refused, and of refused
    parts.

    With ``subject``, only kinds that could say something about it: a refused
    kind that joins nothing a page is about cannot change that page.
    """

    labels = {
        item.spec.label
        for item in readings().unread()
        if subject is None or reading_could_join(item.spec, subject)
    }
    labels.update(
        refused.part.label
        for refused in (part_refusals() if subject is None else refusals_about(subject))
    )
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
        (snapshot, record)
        for snapshot in _inventory(kind)
        if snapshot.reachable
        for record in snapshot.records
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
        try:
            spec = provider.from_record(record)
            names = tuple(provider.hostnames(spec)) if provider.hostnames else ()
            if not names and provider.identity is not None:
                identity = tuple(provider.identity(spec))
                names = identity if len(identity) == 1 else ()
            answers = tuple(provider.answers(spec)) if provider.answers else ()
            origin = provider.origin(spec) if provider.origin else ""
        except (KeyError, TypeError, ValueError):
            continue
        if (
            subject.names(names)
            or subject.holds(answers)
            or (origin and (subject.holds((origin,)) or subject.names((host_of(origin),))))
        ):
            found.append((snapshot, record))
    return tuple(found)


def facts_about(hostnames: Iterable[str], addresses: Iterable[str]) -> tuple[Fact, ...]:
    """Every fact any reading or inventory holds about these names and addresses."""

    subject = Subject.of(hostnames, addresses)
    if not subject:
        return ()
    now = timezone.now()
    found = [
        *_reading_facts(subject),
        *_part_facts(subject),
        *_provider_facts(subject),
        *_container_facts(subject),
        *_tailnet_facts(subject),
    ]
    return tuple(
        _aged(fact, now - stale_after(fact.source_kind))
        for fact in sorted(found, key=lambda f: (f.source_label, f.connection_ref))
    )


def _aged(fact: Fact, stale_before: datetime) -> Fact:
    if fact.state != OBSERVED or fact.observed_at is None:
        return fact
    if fact.observed_at >= stale_before:
        return fact
    return Fact(
        label=fact.label,
        value=fact.value,
        source_kind=fact.source_kind,
        source_label=fact.source_label,
        connection_ref=fact.connection_ref,
        observed_at=fact.observed_at,
        state=STALE,
        detail=fact.detail,
        record=fact.record,
    )


def _joined_kinds() -> tuple[str, ...]:
    """The kinds read here other than the tailnet, which has its own reader."""

    return tuple(OBSERVATIONS) + tuple(
        kind for kind, provider in PROVIDERS.items() if provider.from_record is not None
    ) + (CONTAINER_KIND,)


def _joinable_providers() -> Iterator[tuple[str, Any]]:
    for kind, provider in PROVIDERS.items():
        if provider.from_record is None:
            continue
        if provider.hostnames or provider.answers or provider.origin:
            yield kind, provider


def connection_facts(
    connection_ref: str, provider: str
) -> tuple[tuple[str, str], ...]:
    """What the readings this connection took say about it, through ``ObservationSpec.facts``.

    A record naming no connection speaks for every connection of its provider.
    """

    found: list[tuple[str, str]] = []
    for spec in OBSERVATIONS.values():
        if spec.provider != provider:
            continue
        for snapshot in _inventory(spec.kind):
            if not snapshot.reachable:
                continue
            for record in snapshot.records:
                ref = str(record.get("connection_ref", "") or "")
                if ref and ref != connection_ref:
                    continue
                found.extend(fact for fact in spec.facts(record) if fact not in found)
    return tuple(found)


def snapshots_of(kind: str) -> tuple[Any, ...]:
    """Connected snapshots of one kind the engine reads, from its one read."""

    return _inventory(kind)


def stored_snapshots() -> Mapping[str, tuple[Any, ...]]:
    """Every stored snapshot by kind, connected or not, from the same read."""

    return _stored()


def _stored() -> dict[str, tuple[Any, ...]]:
    """One read of every stored snapshot, shared by the joins and credential sight."""

    from hq.domains.control_plane.models import ProviderInventory

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


def _unreadable(
    snapshot, label: str, requires: str, *, reason: str = "", part: str = "",
    connection_ref: str = "", record: Mapping[str, Any] | None = None,
) -> Fact:
    because = reason or snapshot.error or "The last read failed and gave no reason."
    detail = f"{because} Requires: {requires}." if requires else because
    return Fact(
        label=part or label,
        value="",
        source_kind=snapshot.kind,
        source_label=label,
        connection_ref=connection_ref,
        observed_at=snapshot.observed_at,
        state=UNREADABLE,
        detail=detail,
        record=record,
    )


def _partial(
    snapshot, label: str, requires: str, record: Mapping[str, Any], connection_ref: str,
    readout: Mapping[str, Any] | None = None,
) -> list[Fact]:
    """One unreadable fact per part a record says it could not read."""

    unread = record.get("unread")
    if not unread:
        return []
    parts = unread.items() if isinstance(unread, Mapping) else (("", unread),)
    return [
        _unreadable(
            snapshot,
            label,
            requires,
            reason=str(reason),
            part=str(part),
            connection_ref=connection_ref,
            record=readout,
        )
        for part, reason in parts
    ]


def _frozen(record: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(record))


def _reading_facts(subject: Subject) -> Iterator[Fact]:
    index = readings()
    for item in index.unread():
        if not reading_could_join(item.spec, subject):
            continue
        yield Fact(
            label=item.spec.label,
            value="",
            source_kind=item.spec.kind,
            source_label=item.spec.label,
            observed_at=item.observed_at,
            state=UNREADABLE,
            detail=item.detail,
        )
    for joined in index.about(subject):
        spec = joined.spec
        source = _joined_source(joined)
        if joined.title:
            yield source(joined.relation, joined.title)
        if joined.issuer:
            yield source("Issued by", joined.issuer)
        for name in joined.hostnames:
            yield source("Hostname", name)
        for address in joined.addresses:
            yield source("Address", address)
        yield from _partial(
            SimpleNamespace(kind=spec.kind, observed_at=joined.observed_at, error=""),
            spec.label,
            ", ".join(spec.requires),
            joined.record,
            joined.connection_ref,
            joined.record,
        )
        yield from _record_part_facts(joined)


def _record_part_facts(joined: Joined) -> Iterator[Fact]:
    """The parts refused on this one record, named by its title."""

    title = joined.title.strip().lower()
    if not title:
        return
    for refused in part_refusals((joined.kind,)):
        if refused.scope == title:
            yield _part_fact(refused)


def _joined_source(joined: Joined) -> Callable[..., Fact]:
    def fact(name: str, value: str, detail: str = "") -> Fact:
        return Fact(
            label=name,
            value=str(value),
            source_kind=joined.kind,
            source_label=joined.label,
            connection_ref=joined.connection_ref,
            observed_at=joined.observed_at,
            detail=detail,
            record=joined.record,
        )

    return fact


def _source(
    snapshot, label: str, connection_ref: str, record: Mapping[str, Any] | None = None
) -> Callable[..., Fact]:
    def fact(name: str, value: str, detail: str = "") -> Fact:
        return Fact(
            label=name,
            value=str(value),
            source_kind=snapshot.kind,
            source_label=label,
            connection_ref=connection_ref,
            observed_at=snapshot.observed_at,
            detail=detail,
            record=record,
        )

    return fact


def _requires(provider) -> str:
    """What reading this kind needs, from the connections the provider declares."""

    if not provider.connection_providers:
        return ""
    return f"a reachable {' or '.join(provider.connection_providers)} connection"


def _provider_facts(subject: Subject) -> Iterator[Fact]:
    for kind, provider in _joinable_providers():
        label = kind_label(kind)
        for snapshot in _inventory(kind):
            if not snapshot.reachable:
                yield _unreadable(snapshot, label, _requires(provider))
                continue
            for record in snapshot.records:
                yield from _record_facts(subject, provider, snapshot, label, record)


def _record_facts(subject: Subject, provider, snapshot, label: str, record) -> Iterator[Fact]:
    """What one provider record says about ``subject``, if it is about it."""

    connection_ref = str(record.get("connection_ref", "") or "")
    try:
        names, answers, origin = _record_keys(provider, record)
    except (KeyError, TypeError, ValueError) as exc:
        yield _unreadable(
            snapshot,
            label,
            "",
            reason=f"A record could not be read: {exc!r}.",
            connection_ref=connection_ref,
        )
        return
    about = (
        subject.names(names)
        or subject.holds(answers)
        or (origin and (subject.holds((origin,)) or subject.names((host_of(origin),))))
    )
    if not about:
        return
    source = _source(snapshot, label, connection_ref)
    for name in names:
        yield source("Hostname", normalized_hostname(name))
    for answer in answers:
        yield source("Address", answer, detail=", ".join(names))
    if origin and origin not in answers:
        yield source("Origin", origin, detail=", ".join(names))
    yield from _partial(snapshot, label, _requires(provider), record, connection_ref)


def _record_keys(provider, record) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """The hostnames, answers and origin a provider reads out of one record."""

    spec = provider.from_record(record)
    names = tuple(provider.hostnames(spec)) if provider.hostnames else ()
    answers = tuple(provider.answers(spec)) if provider.answers else ()
    origin = provider.origin(spec) if provider.origin else ""
    return names, answers, origin


def _container_facts(subject: Subject) -> Iterator[Fact]:
    from .containers import Running

    provider = PROVIDERS[CONTAINER_KIND]
    label = provider.label or CONTAINER_KIND
    for snapshot in _inventory(CONTAINER_KIND):
        if not snapshot.reachable:
            yield _unreadable(snapshot, label, _requires(provider))
            continue
        for record in snapshot.records:
            running = Running.of(record, snapshot.observed_at)
            if not (subject.names((running.host,)) or subject.holds((running.host_address,))):
                continue
            source = _source(snapshot, label, running.connection_ref)
            yield source("Container", running.name, detail=running.state)
            if running.host_address:
                yield source("Address", running.host_address)
            yield from _partial(
                snapshot, label, _requires(provider), record, running.connection_ref
            )


def _tailnet_facts(subject: Subject) -> Iterator[Fact]:
    from .tailnet import devices, snapshots

    provider = PROVIDERS[TAILNET_KIND]
    label = provider.label or TAILNET_KIND
    known = devices()
    for snapshot in snapshots()[TAILNET_KIND]:
        if not snapshot.connected:
            continue
        if not snapshot.reachable:
            yield _unreadable(snapshot, label, _requires(provider))
            continue
        for record in snapshot.records:
            device = known.get(str(record.get("name", "")))
            if device is None:
                continue
            if not (
                subject.names((device.name, device.label, device.dns_name))
                or subject.holds(device.addresses)
            ):
                continue
            connection_ref = str(record.get("connection_ref", "") or "")
            source = _source(snapshot, label, connection_ref)
            yield source("Tailnet name", device.label)
            for address in device.addresses:
                yield source("Address", address)
            if device.os:
                yield source("Operating system", device.os)
            yield source("Online", "yes" if device.online else "no")
            yield from _partial(snapshot, label, _requires(provider), record, connection_ref)
