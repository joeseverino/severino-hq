"""The contract every reading is registered under."""

from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, get_args, override

from pydantic import BaseModel, ConfigDict, PlainSerializer, ValidationError, WithJsonSchema

from hq.platform.application.describe import Describe
from hq.platform.application.reasons import Checked, declared_only
from hq.platform.core.digests import digest
lazy from hq.platform.application.json_schemas import json_schema
lazy from hq.platform.application.moments import moment

from ..permissions import Permission
lazy from . import OBSERVATIONS


def _iso(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat() if value is not None else ""


# An instant a provider wrote: read as one when it parses, nothing when it
# does not, and stored as ISO 8601 in UTC, so every reader parses one shape and
# no record carries a provider's own spelling of a time.
Stamp = Annotated[
    datetime | None,
    Checked(moment, mode="before"),
    PlainSerializer(_iso, return_type=str),
    WithJsonSchema({"type": ["string", "null"]}),
]


class ObservationRecord(BaseModel):
    """One observed record. Fields not named on the subclass are dropped.

    A part of a reading that was refused is never a record, nor a field of
    one: the sweep stores it beside the records as a part refusal
    (``control_plane.reading_parts``).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    @classmethod
    @override
    def __pydantic_on_complete__(cls) -> None:
        declared_only(cls.__pydantic_core_schema__, f"Record {cls.__qualname__}")


@dataclass(frozen=True, slots=True)
class ReadingPart:
    """One part a reading is read in, and the permissions that part needs."""

    name: Annotated[str, Describe("The part's name among its type's parts; empty for the whole type.")]
    label: Annotated[str, Describe("The part, in words.")]
    requires: Annotated[
        tuple[Permission, ...], Describe("The permissions reading the part needs, as the service names them.")
    ] = ()
    provider: Annotated[
        str, Describe("The service whose credential reads the part, where it is not its type's own; else empty.")
    ] = ""


def _none(_record: Mapping[str, Any]) -> tuple[str, ...]:
    return ()


def container_key(host: Any, name: Any) -> str:
    """How a container is named across readings: its machine as the provider
    names it, and its name there. Neither can hold a slash."""

    machine, container = str(host or "").strip().lower(), str(name or "").strip()
    return f"{machine}/{container}" if machine and container else ""


# The facets a service is assembled from, in the order its columns render and a
# name is wired in: something has to run before ingress can reach it, and
# ingress before a certificate secures it. A provider names the one it supplies.
ServiceFacet = Literal["runtime", "dns", "proxy", "certificate"]


def _blank(_record: Mapping[str, Any]) -> str:
    return ""


def _nowhere(_record: Mapping[str, Any], _hostname: str) -> str:
    return ""


# What a reading can supply to a subject beyond the relation it states: the
# service facets, a domain's registration, and who holds a network.
ReadingFacet = Literal[ServiceFacet, "registration", "network"]
READING_FACETS: tuple[str, ...] = get_args(ReadingFacet)
# Who takes a reading: a controller through a connection credential, HQ itself
# from a keyless public registry, or HQ from the requests it serves.
Reader = Literal["controller", "hq", "request"]
READERS: tuple[str, ...] = get_args(Reader)


@dataclass(frozen=True, slots=True)
class ObservationSpec:
    kind: str
    # The connection provider whose credential reads it, or None where no one
    # connection does: a controller's report gathered through several.
    provider: str | None
    label: str
    record: type[ObservationRecord]
    # The permissions the read needs, from its provider's table (``permissions``).
    requires: tuple[Permission, ...] = ()
    # The parts it is read in, each with the subset of ``requires`` it needs,
    # where one part can be refused while the others read.
    parts: tuple[ReadingPart, ...] = ()
    # Join keys: the hostnames and addresses a record is about, the
    # containers it names (``container_key``), and the public keys it names,
    # each the key of whatever holds its private half.
    hostnames: Callable[[Mapping[str, Any]], Iterable[str]] = _none
    addresses: Callable[[Mapping[str, Any]], Iterable[str]] = _none
    containers: Callable[[Mapping[str, Any]], Iterable[str]] = _none
    keys: Callable[[Mapping[str, Any]], Iterable[str]] = _none
    # The record's name as a person would say it, and what it is in a few
    # words beside that name ("bridge · 172.21.0.0/16").
    title: Callable[[Mapping[str, Any]], str] = lambda _record: ""
    describe: Callable[[Mapping[str, Any]], str] = _blank
    # What a record is to the subject it joins, as a short present-tense
    # phrase shown beside its title: "Served by Pages project".
    relation: str = ""
    # The phrase when the join was through ``addresses``, through
    # ``containers``, as the container says it, or through ``keys``, as the
    # key's holder says it; ``relation`` if blank.
    address_relation: str = ""
    container_relation: str = ""
    key_relation: str = ""
    # Whether the containers a record names reach each other through it: a
    # network does, unless the runtime made it for every container. Set, the
    # topology draws "talks to" between each pair it names that are declared.
    connects: Callable[[Mapping[str, Any]], bool] | None = None
    # What a joined record supplies, or blank. A service shows a record under
    # the facet it supplies; "runtime" and "network" name what serves an origin.
    facet: ReadingFacet | Literal[""] = ""
    # The label under a column that already names the facet: "Edge" under
    # Certificate. Blank falls back to ``label``.
    short_label: str = ""
    # When the record stops being valid, as ISO 8601, and who vouches for it.
    expires: Callable[[Mapping[str, Any]], str] = _blank
    # When what a record tells of happened (an upload, a run's end), as ISO
    # 8601: the newest of a reading's records is the latest by it.
    at: Callable[[Mapping[str, Any]], str] = _blank
    issuer: Callable[[Mapping[str, Any]], str] = _blank
    read_by: Reader = "controller"
    # How often HQ reads it itself; for a controller's, a clock of its own,
    # slower than the sweep's, for a reading whose provider rations calls: read
    # when it is this old or somebody asks, and carried by every sweep between.
    # None for a controller's reads on every sweep.
    every: timedelta | None = None
    # Read when a page that shows it opens and it is older than ``every``,
    # never on a sweep or a clock.
    on_open: bool = False
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
    facts: Callable[[Mapping[str, Any]], tuple[tuple[str, str], ...]] = lambda _record: ()
    # Whether its records are what people sent (names, addresses, messages),
    # kept for the one page whose capability covers them. The readings any
    # reader may open count them and never list them.
    withheld: bool = False
    # What HQ takes as it stores a reading, from the records it last stored
    # of the kind and the new ones: a rate across two sweeps. It returns the
    # new records with its fields set.
    from_last: Callable[[Iterable[Mapping[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]] | None = None
    # The kind whose read takes this one in the same pass, its records carried
    # when that kind's are: a sweep asks for and carries both or neither
    # (``read_together``). Blank for a kind read on its own.
    read_with: str = ""

    def __post_init__(self) -> None:
        if self.every is not None and self.read_by == "request":
            raise ValueError(f"{self.kind!r}: a request's reading keeps no clock.")
        if self.every is None and self.read_by == "hq":
            raise ValueError(f"{self.kind!r}: a reading HQ takes itself declares how often.")
        if self.on_open and self.every is None:
            raise ValueError(f"{self.kind!r}: a reading taken when a page opens declares how old it may get.")
        if self.connects is not None and self.containers is _none:
            raise ValueError(f"{self.kind!r}: connects needs the containers it names.")
        names = [part.name for part in self.parts]
        if any(not name.strip() for name in names) or len(names) != len(set(names)):
            raise ValueError(f"{self.kind!r}: parts have unique, non-blank names.")
        for part in self.parts:
            if not set(part.requires) <= set(self.requires):
                raise ValueError(f"{self.kind!r}: part {part.name!r} requires more than the reading declares.")
            if part.provider and part.provider != self.provider:
                raise ValueError(f"{self.kind!r}: part {part.name!r} is read by the reading's provider.")

    @override
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

    @property
    def joins_keys(self) -> bool:
        return self.keys is not _none

    @property
    def joins(self) -> bool:
        """Whether a record can join a subject by any key: one that cannot is
        shown only on the connection that read it."""

        return self.joins_hostnames or self.joins_addresses or self.containers is not _none or self.joins_keys

    def relation_to(self, *, by_address: bool, by_container: bool = False, by_key: bool = False) -> str:
        """The phrase for a record joined by key, container, address or hostname."""

        if by_key and self.key_relation:
            return self.key_relation
        if by_container and self.container_relation:
            return self.container_relation
        if by_address and self.address_relation:
            return self.address_relation
        return self.relation or self.label

    def admitted(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """A stored record with only the fields its schema names."""

        fields = self.record.model_fields
        return {name: value for name, value in record.items() if name in fields}

    def fingerprint(self, declared: Collection[str]) -> str:
        """What a stored record of the kind may hold: its schema, and which of
        its names the bridge's door keeps as the contract's own (``declared``).

        A release that moves it changed what the records HQ holds were let
        through as, so the boot asks for the kind to be read again.
        """

        schema = json_schema(self.record)
        names = {*member_names(schema), *(part.name for part in self.parts)}
        held = {"schema": schema, "declared": sorted(names & set(declared))}
        return digest(held)

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


def schema_nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Every object in a JSON document, at any depth, outermost first."""

    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from schema_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from schema_nodes(child)


def member_names(node: Any) -> Iterator[str]:
    """Every member name a JSON schema gives, at any depth: the names the
    bridge's door takes as the contract's own."""

    for found in schema_nodes(node):
        members = found.get("properties")
        if isinstance(members, dict):
            yield from members


def _registered(kind: str) -> ObservationSpec:
    return OBSERVATIONS[kind]


def registry(specs: tuple[ObservationSpec, ...]) -> Mapping[str, ObservationSpec]:
    found: dict[str, ObservationSpec] = {}
    for spec in specs:
        if found.setdefault(spec.kind, spec) is not spec:
            raise ValueError(f"Duplicate observation kind {spec.kind!r}.")
    for spec in found.values():
        root = found.get(spec.read_with) if spec.read_with else spec
        if root is None or root.read_with or root.provider != spec.provider:
            raise ValueError(f"{spec.kind!r} is read with a kind of its provider that is read on its own.")
    return frozendict(found)


def read_together(kinds: Iterable[str]) -> set[str]:
    """``kinds`` and every kind read in the same pass as one of them."""

    asked = set(kinds)
    roots = {(OBSERVATIONS[kind].read_with or kind) if kind in OBSERVATIONS else kind for kind in asked}
    return asked | {kind for kind, spec in OBSERVATIONS.items() if (spec.read_with or kind) in roots}
