"""The errors HQ kept of the requests it failed (``core.server_errors``), read by request id.

A traceback can carry configuration and data, so the read is the audit log's
(``READ_AUDIT_LOG``): an operator's, apart from the baseline READ a viewer and
an agent granted only READ hold.
"""

from typing import Annotated, Any, TypedDict

from hq.platform.core.models import ServerError

from .answer_fields import DateTime, PageUrl
from .describe import Describe
from .doors import Attribution
from .integration_specs import ListRead, ListSort, ResourceSpec
from .projection import guarded, iso, page_of
from .record_errors import RecordNotFound
from .resources import BoundedQuery, SearchText
from .search_contracts import SearchDefinition
from .security import Capability

SERVER_ERRORS = "server_errors"


class ServerErrorQuery(BoundedQuery):
    query: SearchText = None


class ServerErrorItem(TypedDict):
    """One request HQ failed with an unhandled error: where, for whom, and what was raised."""

    request_id: Annotated[str, Describe("The request's id, which its error page printed and its page names it by.")]
    url: PageUrl
    occurred_at: Annotated[str, DateTime(), Describe("When it failed, an ISO 8601 date-time.")]
    method: Annotated[str, Describe("The request's HTTP method.")]
    path: Annotated[str, Describe("The path it asked for, without its query.")]
    by: Annotated[
        str, Describe("Who asked, through which door, as the audit log names them; empty for nobody signed in.")
    ]
    exception: Annotated[str, Describe("The exception raised, by its module and class.")]
    message: Annotated[str, Describe("What the exception said; empty when it said nothing.")]


class ServerErrorRecord(ServerErrorItem):
    """One error whole: its row, and the traceback its log line carries."""

    traceback: Annotated[str, Describe("The traceback, as the log line carries it; it holds no frame's values.")]


def _item(error: ServerError) -> dict[str, Any]:
    who = Attribution(error.user.get_username() if error.user else error.actor, error.interface, through=error.actor)
    return {
        "request_id": error.request_id,
        "url": error.get_absolute_url(),
        "occurred_at": iso(error.occurred_at),
        "method": error.method,
        "path": error.path,
        "by": who.asker if who else "",
        "exception": error.exception,
        "message": error.message,
    }


def list_errors(*, limit: int = 50) -> dict[str, Any]:
    """The errors kept, newest first."""

    page = page_of(guarded(ServerError.objects.select_related("user")), limit)
    return {**page, "items": [_item(error) for error in page["items"]]}


def get_error(request_id: str) -> dict[str, Any]:
    """One error by its request's id."""

    error = ServerError.objects.select_related("user").filter(request_id=request_id).first()
    if error is None:
        raise RecordNotFound("No error is kept for that request id.")
    return {**_item(error), "traceback": error.traceback}


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            SERVER_ERRORS,
            "Errors",
            "The unhandled errors of the requests HQ failed, each by its request id, with its traceback.",
            Capability.READ_AUDIT_LOG,
            listing=ListRead(
                list_errors,
                ServerErrorQuery,
                ServerErrorItem,
                sorts=(
                    ListSort("-occurred_at", "Newest error", ("-occurred_at", "-pk")),
                    ListSort("occurred_at", "Oldest error", ("occurred_at", "pk")),
                ),
            ),
            detail_handler=get_error,
            identifier="request_id",
            record_type=ServerErrorRecord,
            # A request id pasted into HQ's search finds its error; the traceback is never indexed.
            search=SearchDefinition(
                SERVER_ERRORS,
                ServerError,
                "request_id",
                ("request_id", "exception", "message", "path"),
                label="Errors",
                title_field="exception",
                badge_field="method",
                timestamp_field="occurred_at",
                snippet_field="message",
            ),
            web_route="errors:list",
        ),
    )
