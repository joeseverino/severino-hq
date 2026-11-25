"""The table of history: audit events and the moments other sources dated,
each line drawn by the one row the audit log draws."""

from typing import Annotated, Any
from uuid import UUID
lazy from datetime import timedelta

from django import template

from hq.platform.application.describe import Describe
from hq.platform.core.part_examples import DRAWN_AT
from hq.platform.core.template_parts import Example, Part, slotted
lazy from hq.platform.application import history
lazy from hq.platform.application.routes import reverse
lazy from hq.platform.core.models import AuditLog

register = template.Library()

# The held request and the run of the example chain, by id.
_APPROVAL = UUID("00000000-0000-4000-8000-00000000a001")
_RUN = UUID("00000000-0000-4000-8000-00000000a002")


def _example() -> dict[str, Any]:
    """A deploy, and a run of container starts read as one line."""

    started = ("container", "started on example-host-01")
    found = [
        history.Moment(DRAWN_AT - timedelta(minutes=5), "Deploy", "Deployed example-app 1.2.0", url="/example/1/"),
        *(
            history.Moment(
                DRAWN_AT - timedelta(hours=2, minutes=step), "Container", f"example-{step} started", like=started
            )
            for step in range(4)
        ),
    ]
    return {"groups": (history.HistoryGroup(tuple(history.runs(found))),)}


def _chain() -> dict[str, Any]:
    """An agent's held run as a run's page draws its chain: the person's
    approval with the run it queued beneath it, each row naming who did it,
    the queued run "approved by" opening the approval's entry, then the
    controller's end, said as how long after the approval it came."""

    decided = DRAWN_AT - timedelta(minutes=3)
    approval, run = str(_APPROVAL), str(_RUN)
    agent = {"actor": "example-agent", "interface": "mcp", "approved_by": "example-person", "approval": approval}
    rows = (
        AuditLog(
            pk=2,
            action=AuditLog.Action.CREATED,
            object_type="example.run",
            object_id=run,
            object_repr="Example run",
            operation_id=approval,
            created_at=decided,
            metadata=agent,
        ),
        AuditLog(
            pk=1,
            action=AuditLog.Action.UPDATED,
            object_type="example.approval",
            object_id=approval,
            object_repr="Example run on example-host-01",
            operation_id=approval,
            message="State: pending to approved",
            created_at=decided,
            metadata={"actor": "example-person", "interface": "web"},
        ),
    )
    ended = AuditLog(
        pk=3,
        action=AuditLog.Action.DONE,
        object_type="example.run",
        object_id=run,
        object_repr="Example run",
        operation_id=run,
        message="The example ran.",
        created_at=decided + timedelta(seconds=52),
        metadata={"actor": "example-worker", "interface": "controller"},
    )
    approving = history.HistoryLine(decided, events=rows, opens=reverse("core:approval_entry", args=[approval]))
    finished = history.HistoryLine(ended.created_at, events=(ended,), opens="/example/run/", began=decided)
    return {"groups": (history.HistoryGroup((approving, finished)),)}


def history_table(
    context: Any,
    label: Annotated[str, Describe("What the history is of, the name its table is a stop by.")],
    groups: Annotated[Any, Describe("Its lines under their headings, each a `history.HistoryGroup`.")],
    *,
    hide_who: Annotated[bool, Describe("Who did each left out, for a reader who may not read the audit log.")] = False,
) -> dict[str, Any]:
    """A table of audit events (``HISTORY_TABLE``'s rules)."""

    request = context.get("request")
    here = request.path if request is not None else ""
    return {"table": history.table(label, groups, history.columns(hide_who=hide_who), here=here)}


HISTORY_TABLE = Part(
    name="history_table",
    about="A table of history: audit events and dated moments, each line with what it changed folded beneath it.",
    rules="""
    Every table of audit events is this part, its rows the ones the audit log draws (`history.table`,
    drawn by `{% data_table %}`): the audit log, an event's other events, a record's recent
    changes and a run's chain. On a phone only What and When stay, so What's second line says how it
    came out. Each group (`history.HistoryGroup`) is a `tbody` under its heading row where it has a
    label, its count every row of history it draws (`HistoryGroup.count`, which a section heading
    the table reads too), and a group that starts folded away (`folds`) marks each of its rows as
    folded under its heading. An event that changed things heads them: its gutter holds the fold
    rather than the row's own toggle. A fold carries at most `history.SHOWN` of what the event
    changed, and a last row counts the others and links to the list of them all. Each row's User,
    Action and Message are one audit row's: the line's own row, else its newest, so a row names who
    did what it says, its "approved by" opening the approval's entry where one was decided. A chain's
    steps run oldest first, each later one's time how long after the first it came; each step and
    the rows beneath it open the one page that holds the whole step (a run's, an approval's entry,
    else its record's), and a title opening the page the table is drawn on is no link. Its headings
    are `history.COLUMNS`, without who did each where `hide_who` is set.
    """,
    partial="partials/_history_table.html",
    function=history_table,
    example=Example('{% history_table "Example history" groups %}', _example),
    states={"chain": Example('{% history_table "Example events" groups %}', _chain)},
)
slotted(HISTORY_TABLE, library=register)
