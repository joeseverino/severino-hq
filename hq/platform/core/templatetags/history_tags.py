"""The table of history: audit events and the moments other sources dated,
each line drawn by the one row the audit log draws."""

from typing import Annotated, Any
lazy from datetime import timedelta

from django import template

from hq.platform.application.describe import Describe
from hq.platform.core.part_examples import DRAWN_AT
from hq.platform.core.template_parts import Example, Part, slotted
lazy from hq.platform.application import history

register = template.Library()


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


def history_table(
    context: Any,
    label: Annotated[str, Describe("What the history is of, the name its table is a stop by.")],
    groups: Annotated[Any, Describe("Its lines under their headings, each a `history.HistoryGroup`.")],
    *,
    hide_who: Annotated[bool, Describe("Who did each left out, for a reader who may not read the audit log.")] = False,
) -> dict[str, Any]:
    """A table of audit events (``HISTORY_TABLE``'s rules)."""

    return {"table": history.table(label, groups, history.columns(hide_who=hide_who))}


HISTORY_TABLE = Part(
    name="history_table",
    about="A table of history: audit events and dated moments, each line with what it changed folded beneath it.",
    rules="""
    Every table of audit events is this part, its rows the ones the audit log draws (`history.table`,
    drawn by `{% data_table %}`): the audit log, an event's other events and a record's recent
    changes. On a phone only What and When stay, so What's second line says how it came out. Each
    group (`history.HistoryGroup`) is a `tbody` under
    its heading row where it has a label, and a group that starts folded away (`folds`) marks each of
    its rows as folded under its heading. An event that changed things heads them: its gutter holds the
    fold rather than the row's own toggle. A fold carries at most `history.SHOWN` of what the event
    changed, and a last row counts the others and links to the list of them all. Its headings are
    `history.COLUMNS`, without who did each where `hide_who` is set.
    """,
    partial="partials/_history_table.html",
    pieces=("partials/_history_row.html",),
    function=history_table,
    example=Example('{% history_table "Example history" groups %}', _example),
)
slotted(HISTORY_TABLE, library=register)
