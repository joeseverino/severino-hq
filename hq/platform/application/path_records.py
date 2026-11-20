"""The shape the paths resource answers in: a hostname's routes, hop by hop."""

from typing import Annotated, Any, NotRequired, TypedDict

from .describe import Describe
from .json_schemas import Written
from .path_model import Certificate, Evidence, HopCheck
from .record_parts import CheckPart, WrittenLink, WrittenSource


class OverlayPart(TypedDict):
    relation: Annotated[str, Describe("What it is to the hop, such as an access gate.")]
    link: WrittenLink | None


class ObservedPart(TypedDict):
    relation: Annotated[str, Describe("What the reading says the hostname is to it, such as `Served by`.")]
    name: Annotated[str, Describe("What answers for the hostname, by name.")]
    link: WrittenLink | None
    detail: Annotated[str, Describe("A line on what it does for the hostname; empty for none.")]
    source: Annotated[WrittenSource | None, Describe("Where HQ read it; null when nothing says.")]


class RoutePart(TypedDict):
    via: Annotated[str, Describe("What the route starts from, such as one DNS answer.")]
    port: Annotated[int | None, Describe("The port a TCP or UDP forward listens on; null for any other route.")]
    line: Annotated[str, Describe("The route in one line, hop to hop.")]
    hops: Annotated[list[HopRow], Describe("The route's hops, in the order a request takes them.")]


class PathRecord(TypedDict):
    """The path a request for a hostname takes, hop by hop on each route."""

    hostname: Annotated[str, Describe("The hostname the path is walked for.")]
    line: Annotated[str, Describe("The first route in one line, hop to hop; empty when no route was walked.")]
    redirects_to: Annotated[str, Describe("The hostname a route redirects the request to; empty when none does.")]
    machine: Annotated[str, Describe("The machine the first route to reach one ends on; empty when none does.")]
    unread: Annotated[
        list[str], Describe("What HQ could not read or walk, each a sentence; empty when the path is whole.")
    ]
    observed: Annotated[
        list[ObservedPart], Describe("What the readings that name the hostname as something that answers hold.")
    ]
    routes: Annotated[
        list[RoutePart],
        Describe("Each route a request can take, from one DNS answer to what serves the name."),
    ]


# Where a hop is one the calling request walked, which a path of a service has none of.
_WALKED = "Present only on a hop the calling request walked."


class HopRow(TypedDict):
    """One hop of HQ's own path; a hop walked for the calling request adds what it showed there."""

    step: Annotated[str, Describe("What the hop is, such as `dns`, `ingress`, `machine` or `container`.")]
    label: Annotated[str, Describe("The hop in the words its step is headed with.")]
    name: Annotated[
        str, Describe("Which one it is, such as a hostname or a machine's name; empty when `label` says it.")
    ]
    link: Annotated[WrittenLink | None, Describe("The page of what the hop names; null when it has none.")]
    detail: Annotated[
        str, Describe("A line on what it does there, such as where it forwards; empty when there is nothing to add.")
    ]
    source: Annotated[WrittenSource | None, Describe("Where HQ read the hop; null when nothing says.")]
    certificate: Annotated[
        Annotated[dict[str, Any], Written(Certificate)] | None,
        Describe("The certificate the hop presents, or why HQ cannot say; null when it presents none."),
    ]
    unread: Annotated[str, Describe("What HQ could not read about the hop, a sentence; empty when nothing is missing.")]
    overlays: Annotated[list[OverlayPart], Describe("What sits over the hop, such as an access gate.")]
    check: NotRequired[
        Annotated[
            Annotated[dict[str, Any], Written(HopCheck)],
            Describe(
                "Whether what the request showed here agrees with what was read, its `state` `unavailable` "
                f"where the request does not show it. {_WALKED}"
            ),
        ]
    ]
    evidence: NotRequired[
        Annotated[
            list[Annotated[dict[str, Any], Written(Evidence)]],
            Describe(f"What the request carried at the hop. {_WALKED}"),
        ]
    ]
    layers: NotRequired[
        Annotated[list[CheckPart], Describe(f"The checks of the request standard decided at the hop. {_WALKED}")]
    ]
