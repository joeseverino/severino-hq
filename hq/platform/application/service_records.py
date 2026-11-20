"""The shapes the services resource answers in: each service's row, item and record."""

from typing import Annotated, Any, TypedDict

from .answer_fields import PageUrl
from .describe import Describe, listed
from .exposure import LEVELS
from .json_schemas import Written
from .managed_records import HealthPart
from .path_records import PathRecord
from .record_parts import OptionalTone, WrittenLink
from .services import HQ_MARK, LISTED, OBSERVED_MARK, PROSPECT
from .tone import Tone
from .ui import ended
from .whereabouts import Origin


class ServiceRow(TypedDict):
    hostname: Annotated[str, Describe("The service's hostname, which a read of one names it by.")]
    status: Annotated[
        str,
        Describe(
            f"How the service stands by its health rule: {listed((Tone.GOOD, Tone.ATTENTION, Tone.SERIOUS, 'unknown'))}, "
            "the last when nothing reads its health."
        ),
    ]
    status_label: Annotated[str, Describe("The service's health in the words its row shows, such as `Working`.")]
    origin: Annotated[dict[str, Any], Written(Origin)] | None
    project: Annotated[str | None, Describe("The name of the project published at this name; null for none.")]
    faults: Annotated[list[str], Describe("What is not set up right, each a sentence; empty when nothing is.")]


class FacetPart(TypedDict):
    id: Annotated[str, Describe("Which part of the service it is, such as `dns`, `proxy`, `certificate` or `runtime`.")]
    label: Annotated[str, Describe("That part in the words its card is headed with, such as `DNS`.")]
    present: Annotated[bool, Describe("Whether any record of HQ's supplies it.")]
    state: Annotated[
        str,
        Describe(
            f"How the records behind it stand: {listed((Tone.GOOD, Tone.ATTENTION, Tone.SERIOUS))}; empty when none supplies it."
        ),
    ]
    resources: Annotated[list[str], Describe("The keys of HQ's records that supply it.")]


class ServiceItem(ServiceRow):
    """One service name whole, each facet with the records behind it."""

    mark: Annotated[
        str,
        Describe(
            f"`{HQ_MARK}` for HQ's own name, `{OBSERVED_MARK}` for a name only a reading holds, "
            "empty for any other service."
        ),
    ]
    facets: Annotated[
        list[FacetPart],
        Describe("Each part the service is made of, such as its DNS record, proxy, certificate and container."),
    ]


class SummaryPart(TypedDict):
    """One fact of the service's summary: what it is, where it runs, whether it is healthy."""

    label: Annotated[str, Describe("What the fact is, such as `Health` or `Certificate`.")]
    value: Annotated[str, Describe("The fact, in a few words.")]
    link: Annotated[WrittenLink | None, Describe("The page of what `value` names; null for none.")]
    detail: Annotated[list[str], Describe("What backs it, each a line.")]
    tone: OptionalTone
    icon: Annotated[str, Describe("The kind of fact it is, as a symbol's name, such as `activity`.")]


class AudiencePart(TypedDict):
    id: Annotated[str, Describe("Who may open a connection, by id, such as `tailnet` or `internet`.")]
    label: Annotated[str, Describe("That audience in words; empty when unknown.")]
    detail: Annotated[str, Describe("What said so, in a sentence.")]
    tailnet_only: Annotated[bool, Describe("Whether only devices on the tailnet may connect.")]


class ExposurePart(TypedDict):
    """Who can reach the name, by the worst of its routes."""

    level: Annotated[str, Describe(ended(f"How open its worst route is: {listed(LEVELS)}"))]
    label: Annotated[str, Describe("That level, in words.")]
    tone: Annotated[str, Describe(ended(f"How that level is colored: {listed(Tone)}"))]
    reach: Annotated[AudiencePart, Describe("Who its worst route lets in.")]
    path_gates: Annotated[
        list[str], Describe("The paths a gate stands in front of on an open route that no gate covers whole.")
    ]


class AliasRecordPart(TypedDict):
    alias: Annotated[str, Describe("The other name the record is for.")]
    link: WrittenLink
    kind: Annotated[str, Describe("The record's type, such as `cloudflare.dns_record`.")]
    health: HealthPart


class ServiceRecord(ServiceItem):
    """One service name whole and the path a request for it takes."""

    url: PageUrl
    listing: Annotated[
        str,
        Describe(
            f"`{LISTED}` for a listed service, `{PROSPECT}` for a hostname nothing serves yet, whose facets "
            "each read as not set up."
        ),
    ]
    path: PathRecord
    summary: Annotated[list[SummaryPart], Describe("What it is, where it runs, and whether it is healthy.")]
    exposure: ExposurePart
    aliases: Annotated[list[str], Describe("The other names that lead to it.")]
    alias_records: Annotated[list[AliasRecordPart], Describe("HQ's records behind those other names.")]
    provider_answers: Annotated[
        str, Describe("The DNS service that answers for the name itself, where a proxied record puts it in front.")
    ]
