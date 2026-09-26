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

from control_plane.names import in_zone
from control_plane.observations import OBSERVATIONS, ObservationSpec
from control_plane.providers import (
    CONTAINER_KIND,
    PROVIDERS,
    certificate_covers,
    expiry_phrase,
    normalized_hostname,
)

from .entity_links import kind_label
from .locate import host_of
from .projection import read_once
from .tailnet import TAILNET_KIND

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

    ``zones`` names every hostname under each zone, the zone included.
    """

    hostnames: frozenset[str]
    addresses: frozenset[str]
    zones: frozenset[str] = frozenset()

    @classmethod
    def of(
        cls,
        hostnames: Iterable[str] = (),
        addresses: Iterable[str] = (),
        zones: Iterable[str] = (),
    ) -> "Subject":
        return cls(
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
        return bool(self.hostnames or self.addresses or self.zones)

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
        return not self.hostnames

    @property
    def relation(self) -> str:
        return self.spec.relation_to(by_address=self.by_address)

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
        for index, entry in enumerate(self._entries):
            for name in entry.hostnames:
                self._by_name.setdefault(name, []).append(index)
            for address in entry.addresses:
                self._by_address.setdefault(address, []).append(index)

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
            if not (names or addresses):
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
                )
            )
        return tuple(joined)

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

    stamp = str(record.get("read_at", "") or "")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


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


def unreadable_labels() -> tuple[str, ...]:
    """The labels of every joined kind whose last read was refused."""

    labels = {item.spec.label for item in readings().unread()}
    for kind, provider in PROVIDERS.items():
        if provider.from_record is None and kind != CONTAINER_KIND:
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
        *_provider_facts(subject),
        *_container_facts(subject),
        *_tailnet_facts(subject),
    ]
    return tuple(
        _aged(fact, now - stale_after(fact.source_kind))
        for fact in sorted(found, key=lambda f: (f.source_label, f.connection_ref))
    )


def disagreements(facts: Iterable[Fact]) -> dict[Fact, tuple[str, ...]]:
    """Observed facts another source reports with a different single value.

    A label is compared only where every source reporting it gives exactly one
    value: a machine has several addresses, and two sources naming different
    ones do not disagree. Maps each such fact to the labels of the sources that
    report the label differently.
    """

    by_label: dict[str, dict[tuple[str, str], set[str]]] = {}
    labels: dict[tuple[str, str], str] = {}
    observed = [fact for fact in facts if fact.state != UNREADABLE]
    for fact in observed:
        by_label.setdefault(fact.label, {}).setdefault(fact.source, set()).add(fact.value)
        labels[fact.source] = fact.source_label
    single = {
        label
        for label, sources in by_label.items()
        if len(sources) > 1 and all(len(values) == 1 for values in sources.values())
    }
    found: dict[Fact, tuple[str, ...]] = {}
    for fact in observed:
        if fact.label not in single:
            continue
        others = tuple(
            sorted(
                {
                    labels[source]
                    for source, values in by_label[fact.label].items()
                    if source != fact.source and fact.value not in values
                }
            )
        )
        if others:
            found[fact] = others
    return found


def stale_after(kind: str = ""):
    """How old a reading of ``kind`` may be before it is stale.

    A controller reads on the sweep; HQ reads the public registries daily.
    """

    from .cadence import slowest_sweep_interval

    spec = OBSERVATIONS.get(kind)
    if spec is not None and spec.read_by == "hq":
        from .public_registry import REFRESH_AFTER

        return 2 * REFRESH_AFTER
    return slowest_sweep_interval()


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


def snapshots_of(kind: str) -> tuple[Any, ...]:
    """Connected snapshots of one kind the engine reads, from its one read."""

    return _inventory(kind)


def _inventory(kind: str) -> tuple[Any, ...]:
    """Snapshots of one kind, from one read of every kind this module joins."""

    from control_plane.models import ProviderInventory

    def load() -> dict[str, tuple[Any, ...]]:
        grouped: dict[str, list[Any]] = {}
        for snapshot in ProviderInventory.objects.filter(
            kind__in=_joined_kinds(), connected=True
        ):
            grouped.setdefault(snapshot.kind, []).append(snapshot)
        return {name: tuple(rows) for name, rows in grouped.items()}

    return read_once(_INVENTORY_KEY, load).get(kind, ())


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
    from .services import Running

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
