"""The contract every reading is registered under."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class ObservationRecord(BaseModel):
    """One observed record. Fields not named on the subclass are dropped.

    A part of a reading that was refused is never a record, nor a field of
    one: the sweep stores it beside the records as a part refusal
    (``control_plane.reading_parts``).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)


@dataclass(frozen=True, slots=True)
class ReadingPart:
    """One part a reading is read in, and the permissions that part needs.

    ``provider`` is the connection provider whose credential reads the part,
    when it is not the kind's own.
    """

    name: str
    label: str
    requires: tuple[str, ...] = ()
    provider: str = ""


def _none(_record: Mapping[str, Any]) -> tuple[str, ...]:
    return ()


def container_key(host: Any, name: Any) -> str:
    """How a container is named across readings: its machine as the provider
    names it, and its name there. Neither can hold a slash."""

    machine, container = str(host or "").strip().lower(), str(name or "").strip()
    return f"{machine}/{container}" if machine and container else ""


def _blank(_record: Mapping[str, Any]) -> str:
    return ""


def _nowhere(_record: Mapping[str, Any], _hostname: str) -> str:
    return ""


# What a reading can supply to a subject beyond the relation it states: the
# service facets, a domain's registration, and who holds a network.
READING_FACETS = frozenset({"runtime", "dns", "proxy", "certificate", "registration", "network"})
# Who takes a reading: a controller through a connection credential, HQ itself
# from a keyless public registry, or HQ from the requests it serves.
READERS = ("controller", "hq", "request")


@dataclass(frozen=True, slots=True)
class ObservationSpec:
    kind: str
    # The connection provider whose credential reads it.
    provider: str
    label: str
    record: type[ObservationRecord]
    # The permissions the read needs, each one exact name the provider uses.
    requires: tuple[str, ...] = ()
    # The parts it is read in, each with the subset of ``requires`` it needs,
    # where one part can be refused while the others read.
    parts: tuple[ReadingPart, ...] = ()
    # Join keys: the hostnames and addresses a record is about, and the
    # containers it names (``container_key``).
    hostnames: Callable[[Mapping[str, Any]], Iterable[str]] = _none
    addresses: Callable[[Mapping[str, Any]], Iterable[str]] = _none
    containers: Callable[[Mapping[str, Any]], Iterable[str]] = _none
    # The record's name as a person would say it, and what it is in a few
    # words beside that name ("bridge · 172.21.0.0/16").
    title: Callable[[Mapping[str, Any]], str] = lambda record: ""
    describe: Callable[[Mapping[str, Any]], str] = _blank
    # What a record is to the subject it joins, as a short present-tense
    # phrase shown beside its title: "Served by Pages project".
    relation: str = ""
    # The phrase when the join was through ``addresses``, or through
    # ``containers``, as the container says it; ``relation`` if blank.
    address_relation: str = ""
    container_relation: str = ""
    # Whether the containers a record names reach each other through it: a
    # network does, unless the runtime made it for every container. Set, the
    # topology draws "talks to" between each pair it names that are declared.
    connects: Callable[[Mapping[str, Any]], bool] | None = None
    # What a joined record supplies, from ``READING_FACETS``, or blank. A
    # service shows a record under the facet it supplies; "runtime" and
    # "network" name what serves an origin.
    facet: str = ""
    # The label under a column that already names the facet: "Edge" under
    # Certificate. Blank falls back to ``label``.
    short_label: str = ""
    # When the record stops being valid, as ISO 8601, and who vouches for it.
    expires: Callable[[Mapping[str, Any]], str] = _blank
    issuer: Callable[[Mapping[str, Any]], str] = _blank
    # One of ``READERS``.
    read_by: str = "controller"
    # A clock of its own, slower than the sweep's, for a reading whose provider
    # rations calls: read when it is this old or somebody asks, and carried by
    # every sweep between. None reads on every sweep.
    every: timedelta | None = None
    # The provider console page for a record, built only from ids the record
    # stores (an account id, a name). Blank when the record lacks them.
    console: Callable[[Mapping[str, Any]], str] = _blank
    # A resource kind whose record must front a hostname for this reading to
    # apply to it by name: an edge certificate serves a name only through a
    # proxied Cloudflare record. A zone subject is not narrowed. Blank: the
    # hostname key alone joins.
    fronted_by: str = ""
    # Whether a record's hostnames are names something answers at, so each
    # is a service even where nothing declares it.
    names_services: bool = False
    # The hostname a record sends its names to, or "": a redirect.
    redirects_to: Callable[[Mapping[str, Any]], str] = _blank
    # Where a record hands one of its hostnames on, as "host:port" or a URL,
    # or "": a tunnel's ingress service.
    upstream: Callable[[Mapping[str, Any], str], str] = _nowhere
    # A header a request carries once it has passed through what a record
    # describes: an Access application's signed assertion.
    request_header: str = ""
    # Whether a record stands in front of the hostnames it names and admits
    # only whom it allows: an Access application, an access list. A name one
    # restricts is reachable from the internet only through that gate.
    restricts: bool = False
    # What a record tells about the connection that read it, as ``(key, value)``
    # facts the findings read off that connection's node.
    facts: Callable[[Mapping[str, Any]], tuple[tuple[str, str], ...]] = lambda record: ()

    def __reduce__(self) -> tuple[Any, tuple[str]]:
        # A spec is a declaration, not data. A stored value that refers to one
        # keeps its kind, and loading it finds the registered spec again.
        return (_registered, (self.kind,))

    @property
    def joins_hostnames(self) -> bool:
        return self.hostnames is not _none

    @property
    def redirects(self) -> bool:
        return self.redirects_to is not _blank

    @property
    def forwards(self) -> bool:
        return self.upstream is not _nowhere

    @property
    def joins_addresses(self) -> bool:
        return self.addresses is not _none

    @property
    def short(self) -> str:
        return self.short_label or self.label

    def relation_to(self, *, by_address: bool, by_container: bool = False) -> str:
        """The phrase for a record joined by container, address or hostname."""

        if by_container and self.container_relation:
            return self.container_relation
        if by_address and self.address_relation:
            return self.address_relation
        return self.relation or self.label

    def admitted(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """A stored record with only the fields its schema names."""

        fields = self.record.model_fields
        return {name: value for name, value in record.items() if name in fields}

    def clean(self, records: Iterable[Any]) -> tuple[list[dict[str, Any]], int]:
        """The records as the schema admits them, and how many it refused."""

        kept: list[dict[str, Any]] = []
        refused = 0
        for record in records:
            try:
                kept.append(self.record.model_validate(record).model_dump(mode="json", exclude_unset=True))
            except ValidationError:
                refused += 1
        return kept, refused


def _registered(kind: str) -> ObservationSpec:
    from . import OBSERVATIONS

    return OBSERVATIONS[kind]


def registry(specs: tuple[ObservationSpec, ...]) -> Mapping[str, ObservationSpec]:
    found: dict[str, ObservationSpec] = {}
    for spec in specs:
        if spec.kind in found:
            raise ValueError(f"Duplicate observation kind {spec.kind!r}.")
        if isinstance(spec.requires, str) or any(not name.strip() or ";" in name for name in spec.requires):
            raise ValueError(f"{spec.kind!r}: requires is a tuple of permission names.")
        if spec.facet and spec.facet not in READING_FACETS:
            raise ValueError(f"{spec.kind!r}: unknown facet {spec.facet!r}.")
        if spec.read_by not in READERS:
            raise ValueError(f"{spec.kind!r}: read_by is one of {READERS}.")
        if spec.every is not None and spec.read_by != "controller":
            raise ValueError(f"{spec.kind!r}: only a controller's reading keeps its own clock.")
        if spec.connects is not None and spec.containers is _none:
            raise ValueError(f"{spec.kind!r}: connects needs the containers it names.")
        _check_parts(spec)
        found[spec.kind] = spec
    return MappingProxyType(found)


def _check_parts(spec: ObservationSpec) -> None:
    names = [part.name for part in spec.parts]
    if any(not name.strip() for name in names) or len(names) != len(set(names)):
        raise ValueError(f"{spec.kind!r}: parts have unique, non-blank names.")
    for part in spec.parts:
        if not set(part.requires) <= set(spec.requires):
            raise ValueError(f"{spec.kind!r}: part {part.name!r} requires more than the reading declares.")
        if part.provider and part.provider != spec.provider:
            raise ValueError(f"{spec.kind!r}: part {part.name!r} is read by the reading's provider.")
