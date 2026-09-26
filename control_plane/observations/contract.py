"""The contract every reading is registered under."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class ObservationRecord(BaseModel):
    """One observed record. Fields not named on the subclass are dropped."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    # Why part of the record could not be read: one reason, or one per part.
    unread: str | dict[str, str] | None = None


def _none(_record: Mapping[str, Any]) -> tuple[str, ...]:
    return ()


def _blank(_record: Mapping[str, Any]) -> str:
    return ""


# What a reading can supply to a subject beyond the relation it states: the
# service facets, a domain's registration, and who holds a network.
READING_FACETS = frozenset(
    {"runtime", "dns", "proxy", "certificate", "registration", "network"}
)
# Who takes a reading: a controller through a connection credential, or HQ
# itself from a keyless public registry.
READERS = ("controller", "hq")


@dataclass(frozen=True)
class ObservationSpec:
    kind: str
    # The connection provider whose credential reads it.
    provider: str
    label: str
    record: type[ObservationRecord]
    # The permissions the read needs, each one exact name the provider uses.
    # A part that needs more says so in the record's `unread`.
    requires: tuple[str, ...] = ()
    # Join keys: the hostnames and addresses a record is about.
    hostnames: Callable[[Mapping[str, Any]], Iterable[str]] = _none
    addresses: Callable[[Mapping[str, Any]], Iterable[str]] = _none
    # The record's name as a person would say it.
    title: Callable[[Mapping[str, Any]], str] = lambda record: ""
    # What a record is to the subject it joins, as a short present-tense
    # phrase shown beside its title: "Served by Pages project".
    relation: str = ""
    # The phrase when the join was through ``addresses``; ``relation`` if blank.
    address_relation: str = ""
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
    # The provider console page for a record, built only from ids the record
    # stores (an account id, a name). Blank when the record lacks them.
    console: Callable[[Mapping[str, Any]], str] = _blank
    # A resource kind whose record must front a hostname for this reading to
    # apply to it by name: an edge certificate serves a name only through a
    # proxied Cloudflare record. A zone subject is not narrowed. Blank: the
    # hostname key alone joins.
    fronted_by: str = ""

    @property
    def joins_hostnames(self) -> bool:
        return self.hostnames is not _none

    @property
    def joins_addresses(self) -> bool:
        return self.addresses is not _none

    @property
    def short(self) -> str:
        return self.short_label or self.label

    def relation_to(self, *, by_address: bool) -> str:
        """The phrase for a record joined by address or by hostname."""

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
                kept.append(
                    self.record.model_validate(record).model_dump(
                        mode="json", exclude_unset=True
                    )
                )
            except ValidationError:
                refused += 1
        return kept, refused


def registry(specs: tuple[ObservationSpec, ...]) -> Mapping[str, ObservationSpec]:
    found: dict[str, ObservationSpec] = {}
    for spec in specs:
        if spec.kind in found:
            raise ValueError(f"Duplicate observation kind {spec.kind!r}.")
        if isinstance(spec.requires, str) or any(
            not name.strip() or ";" in name for name in spec.requires
        ):
            raise ValueError(f"{spec.kind!r}: requires is a tuple of permission names.")
        if spec.facet and spec.facet not in READING_FACETS:
            raise ValueError(f"{spec.kind!r}: unknown facet {spec.facet!r}.")
        if spec.read_by not in READERS:
            raise ValueError(f"{spec.kind!r}: read_by is one of {READERS}.")
        found[spec.kind] = spec
    return MappingProxyType(found)
