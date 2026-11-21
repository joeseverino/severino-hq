"""The audit log, as a searchable resource read a page at a time."""

from datetime import date
from typing import Annotated, Any, TypedDict

from django.db.models import Q, QuerySet
from pydantic import Field

from hq.platform.core.models import AuditLog
lazy from hq.platform.core.audit import ROUTINE_EVENTS

from . import history
from .answer_fields import PageUrl
from .command_fields import Day, RequiredText
from .describe import Describe, listed
from .integration_specs import ListFilter, ListRead, ListSort, ListToggle, ResourceSpec
from .projection import day_span, guarded, iso, page_of
from .record_errors import RecordNotFound
from .record_fields import RecordId
from .resources import BoundedQuery, SearchText
from .routes import reverse
from .search_contracts import SearchDefinition
from .security import Capability
from .ui import ended
lazy from .approvals import AUDIT_LABEL, awaiting_ids
lazy from .record_timeline import record_timeline


class AuditQuery(BoundedQuery):
    query: SearchText = None
    about: RequiredText | None = Field(
        default=None, max_length=200, description="Only the rows about the record with this key; absent, every row."
    )
    on: Day | None = Field(default=None, description="Only the rows of this day, in HQ's time zone; absent, every day.")
    awaiting: bool = Field(default=False, description="True for only the requests still waiting for an operator.")
    events: bool = Field(
        default=False,
        description="True for one row per operation, its newest of those kept, in place of every row it wrote.",
    )


class AuditItem(TypedDict):
    """One row of the log: what was done to what, by whom, and where."""

    id: Annotated[int, Describe("The row's id, which its page names it by.")]
    url: PageUrl
    created_at: Annotated[str, Describe("When the row was written, an ISO 8601 date-time.")]
    action: Annotated[str, Describe(ended(f"What was done: {listed(AuditLog.Action.values)}"))]
    action_label: Annotated[str, Describe("What was done, in the words the log says it in.")]
    object_type: Annotated[str, Describe("The kind of record it was done to; empty for none.")]
    object_id: Annotated[str, Describe("That record's id; empty for none.")]
    object_repr: Annotated[str, Describe("What that record was called when it was done.")]
    message: Annotated[str, Describe("What it says happened; empty when it says nothing more.")]
    actor: Annotated[str, Describe("Who or what acted; empty for nobody recorded.")]
    source: Annotated[str, Describe(ended(f"Where it was recorded: {listed(dict(history.SOURCES))}"))]
    operation_id: Annotated[str, Describe("The operation it was written in; empty for a row of none.")]


def _item(event: AuditLog) -> dict[str, Any]:
    return {
        "id": event.pk,
        "url": reverse("core:audit_detail", args=[event.pk]),
        "created_at": iso(event.created_at),
        "action": event.action,
        "action_label": event.get_action_display(),
        "object_type": event.object_type,
        "object_id": event.object_id,
        "object_repr": event.object_repr,
        "message": event.message,
        "actor": event.who.actor,
        # The annotation audit_rows adds; the model has no such field.
        "source": vars(event)["source"],
        "operation_id": event.operation_id,
    }


def audit_rows(*, about: str | None = None, on: date | None = None, awaiting: bool = False) -> QuerySet[AuditLog]:
    """The rows of the log one record, one day or the approval queue narrows
    it to, each with where it was recorded."""

    rows = record_timeline(about) if about else AuditLog.objects.all()
    rows = rows.select_related("user").annotate(source=history.source_of_event())
    if awaiting:
        rows = rows.filter(action=AuditLog.Action.CREATED, object_type=AUDIT_LABEL, object_id__in=awaiting_ids())
    if on is not None:
        start, end = day_span(on, on)
        rows = rows.filter(created_at__gte=start, created_at__lt=end)
    return rows


def list_audit(
    *, about: str | None = None, on: date | None = None, awaiting: bool = False, events: bool = False, limit: int = 50
) -> dict[str, Any]:
    """The log a page at a time, newest first, never counted."""

    rows = guarded(audit_rows(about=about, on=on, awaiting=awaiting))
    page = page_of(rows, limit, then=history.heads if events else None)
    return {**page, "items": [_item(event) for event in page["items"]]}


def get_event(event_id: int) -> dict[str, Any]:
    """One row of the log by its id."""

    event = audit_rows().filter(pk=event_id).first()
    if event is None:
        raise RecordNotFound("No audit row has that id.")
    return _item(event)


def _recorded(object_type: str, action: str = AuditLog.Action.UPDATED) -> tuple[tuple[int], tuple[()]]:
    """A row the log leaves out until its toggle is on, kept."""

    return (AuditLog.objects.create(action=action, object_type=object_type, object_repr="Example").pk,), ()


def _routine() -> tuple[tuple[int], tuple[()]]:
    action, object_type = next(iter(ROUTINE_EVENTS))
    return _recorded(object_type, action)


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "audit",
            "Audit log",
            "Security-sensitive audit events indexed by HQ.",
            Capability.READ_AUDIT_LOG,
            listing=ListRead(
                list_audit,
                AuditQuery,
                AuditItem,
                sorts=(
                    ListSort("-created_at", "Newest event", ("-created_at", "-pk")),
                    ListSort("created_at", "Oldest event", ("created_at", "pk")),
                    *ListSort.both("object_type", "Type"),
                    *ListSort.both("user__username", "User"),
                    *ListSort.both("action", "Action"),
                    *ListSort.both("message", "Message"),
                ),
                filters=(
                    ListFilter("source", "Where", history.SOURCES),
                    ListFilter("action", "Action", AuditLog.Action.choices),
                ),
                # Off until asked for: an ask for a fresh reading, or routine work, changes nothing.
                toggles=(
                    ListToggle(
                        "reads",
                        "Read requests",
                        "True for the asks for a fresh reading too; absent, they are left out.",
                        Q(),
                        lambda: _recorded(history.read_request_type()),
                        unasked=~history.read_requests(),
                    ),
                    ListToggle(
                        "routine",
                        "Routine work",
                        "True for the work HQ did on its own that changed nothing too; absent, it is left out.",
                        Q(),
                        _routine,
                        unasked=~history.routine(),
                    ),
                ),
                counted=False,
            ),
            detail_handler=get_event,
            identifier="id",
            identifier_type=RecordId,
            record_type=AuditItem,
            search=SearchDefinition(
                "audit",
                AuditLog,
                "pk",
                ("action", "object_type", "object_id", "object_repr", "operation_id", "message"),
                label="Audit log",
                title_field="search_title",
                badge_field="type_label",
                timestamp_field="created_at",
                snippet_field="search_snippet",
            ),
            web_route="core:audit_list",
        ),
    )
