"""The shapes the services resource answers in: each service's row, item and record."""

from typing import Annotated, Any, TypedDict

from .answer_fields import PageUrl
from .describe import Describe, listed
from .json_schemas import Written
from .path_records import PathRecord
from .services import HQ_MARK, LISTED, OBSERVED_MARK, PROSPECT
from .tone import Tone
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
