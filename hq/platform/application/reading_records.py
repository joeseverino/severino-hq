"""The shapes the readings resource answers in: each type of reading's row, item and record."""

from typing import Annotated, TypedDict

from hq.domains.control_plane.observations.contract import READERS, READING_FACETS
from hq.domains.control_plane.provider_adapters.contracts import REFUSALS

from .answer_fields import AsRead, ObservedAt
from .credential_sight import SightState
from .describe import Describe, listed
from .ui import ended


class ReadingRow(TypedDict):
    kind: Annotated[str, Describe("The reading's type, such as `adguard.client`, which a read of one names it by.")]
    label: Annotated[str, Describe("The reading's name, in words.")]
    provider: Annotated[
        str | None, Describe("The service it is read from, such as `adguard`; null where no one connection reads it.")
    ]
    state: Annotated[str, Describe(ended(f"How its last read went: {listed(SightState)}"))]
    records: Annotated[int, Describe("How many records the last read stored.")]
    observed_at: ObservedAt | None
    error: Annotated[str, Describe("Why the last read failed, as the service said; empty when it did not.")]


class Aged(TypedDict):
    """How current a kind's last read is, as ``credential_sight.Standing`` says it."""

    freshness: Annotated[str, Describe("How current its last read is, in one word.")]
    freshness_label: Annotated[str, Describe("How current its last read is, in the words its row shows.")]


class ReadingItem(ReadingRow, Aged):
    provider_label: Annotated[str, Describe("The service's name, in words.")]
    read_by: Annotated[
        str,
        Describe(
            f"Who takes the reading, {listed(READERS)}: a controller through a connection's credential, HQ from "
            "a public registry, or HQ from the requests it serves."
        ),
    ]
    facet: Annotated[
        str, Describe(f"What its records supply a service: {listed(sorted(READING_FACETS))}, or empty for none.")
    ]
    relation: Annotated[
        str,
        Describe(
            "What a record is to what it joins, a short phrase such as `Served by Pages project`; empty for none."
        ),
    ]
    requires: Annotated[list[str], Describe("The permissions the read needs, each as the service names it.")]
    refusal: Annotated[
        str,
        Describe(
            f"Whether the service refused the credential or one permission, {listed(REFUSALS)}; empty when neither."
        ),
    ]
    remedy: Annotated[
        str, Describe("What would let HQ read it, such as the permissions to add; empty when nothing is missing.")
    ]


class ReadingRecord(ReadingItem):
    """One type of reading with the records it holds, as its schema admits them."""

    items: Annotated[
        list[AsRead],
        Describe("The records it holds, each as its type's schema admits it; empty for a type HQ withholds."),
    ]
