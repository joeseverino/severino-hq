"""The events HQ was told of, as a readable resource: the one list its page,
the API and MCP's ``list_resource`` read."""

from typing import Annotated, Any, TypedDict

from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
from hq.domains.control_plane.models import InboundEvent, InboundOutcome, InboundSource

from .answer_fields import DateTime
from .describe import Describe, listed
from .integration_specs import ListFilter, ListRead, ListSort, ResourceSpec
from .labels import human_label
from .projection import guarded, iso, page_of
from .record_errors import RecordNotFound
from .record_fields import RecordId
from .resources import BoundedQuery, SearchText
from .search_contracts import SearchDefinition
from .security import Capability
from .ui import ended

INBOUND_EVENTS = "inbound_events"


class InboundEventQuery(BoundedQuery):
    query: SearchText = None


class InboundEventItem(TypedDict):
    """One event a provider or HQ's host said happened, and what it made due."""

    id: Annotated[int, Describe("The event's id in HQ.")]
    provider: Annotated[str, Describe("The provider that sent it, by its name in HQ's declarations.")]
    provider_label: Annotated[str, Describe("That provider, as HQ's pages name it.")]
    event: Annotated[
        str, Describe("The provider's name for the event, with its action after a dot where the action matters.")
    ]
    subject: Annotated[str, Describe("What it is about, as the provider names it; empty when it names nothing.")]
    delivery_id: Annotated[str, Describe("The provider's id for the delivery, the same on each retry of it.")]
    source: Annotated[str, Describe(ended(f"Which channel told HQ: {listed(InboundSource.values)}"))]
    outcome: Annotated[
        str,
        Describe(
            ended(
                f"`{InboundOutcome.APPLIED}` where it made readings due, "
                f"`{InboundOutcome.IGNORED}` for an event HQ does not take"
            )
        ),
    ]
    reads: Annotated[list[str], Describe("The reading kinds it made due; empty for an ignored event.")]
    received_at: Annotated[str, DateTime(), Describe("When the provider's request arrived, an ISO 8601 date-time.")]
    applied_at: Annotated[str, DateTime(), Describe("When HQ stored it, an ISO 8601 date-time.")]


def _item(event: InboundEvent) -> dict[str, Any]:
    return {
        "id": event.pk,
        "provider": event.provider,
        "provider_label": CONNECTION_LABELS.get(event.provider, human_label(event.provider)),
        "event": event.event,
        "subject": event.subject,
        "delivery_id": event.delivery_id,
        "source": event.source,
        "outcome": event.outcome,
        "reads": list(event.reads),
        "received_at": iso(event.received_at),
        "applied_at": iso(event.applied_at),
    }


def list_events(*, limit: int = 50) -> dict[str, Any]:
    """The events kept, newest first."""

    return page_of(guarded(InboundEvent.objects.all()), limit, each=_item)


def get_event(identifier: int) -> dict[str, Any]:
    """One event by its id."""

    event = InboundEvent.objects.filter(pk=identifier).first()
    if event is None:
        raise RecordNotFound("HQ keeps no event by that id.")
    return _item(event)


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            INBOUND_EVENTS,
            "Events",
            "What providers and HQ's host told HQ happened, each delivery once, and the readings each made due.",
            Capability.READ,
            listing=ListRead(
                list_events,
                InboundEventQuery,
                InboundEventItem,
                sorts=(
                    ListSort("-received_at", "Newest event", ("-received_at", "-pk")),
                    ListSort("received_at", "Oldest event", ("received_at", "pk")),
                ),
                filters=(
                    ListFilter("source", "Source", InboundSource.choices),
                    ListFilter("outcome", "Outcome", InboundOutcome.choices),
                ),
            ),
            detail_handler=get_event,
            identifier="id",
            identifier_type=RecordId,
            web_route="control_plane:events",
            record_type=InboundEventItem,
            # A delivery id pasted from the provider's log finds its event.
            search=SearchDefinition(
                INBOUND_EVENTS,
                InboundEvent,
                "pk",
                ("event", "subject", "provider", "delivery_id"),
                label="Events",
                badge_field="source",
                timestamp_field="received_at",
            ),
        ),
    )
