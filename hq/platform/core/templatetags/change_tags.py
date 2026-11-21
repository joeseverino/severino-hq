"""The change table: what a change moves, each value before and after it."""

from typing import Annotated, Any, NamedTuple

from django import template
from django.utils.safestring import SafeString
from django.utils.text import slugify

from hq.platform.application.describe import Describe
from hq.platform.application.payload_words import ChangeRow
from hq.platform.application.tone import Tone
from hq.platform.core.part_examples import tags
from hq.platform.core.template_parts import Part, slotted
from hq.platform.core.templatetags.status_tags import status_pill
lazy from hq.platform.application.approval_review import ChangeGroup, ChangePreview

register = template.Library()


class _Kind(NamedTuple):
    made: str
    held: str
    tone: Tone


# What a row's kind of change is called: as a change made says it, and as one
# held for approval says what it would do (a changed row is pending). Its tone: what comes is good news,
# what moves asks attention, what goes is serious. A record that comes or goes
# whole with no summary of its own says the same on its row.
_KINDS = {
    "added": _Kind("New", "Would add", Tone.GOOD),
    "changed": _Kind("Changed", "Pending", Tone.ATTENTION),
    "removed": _Kind("Removed", "Would remove", Tone.SERIOUS),
}


def _words(kind: str, preview: ChangePreview | None) -> str:
    """What ``kind`` is called in ``preview``: what it did once made, else
    what approving it would do, as a table of no preview is held too."""

    said = _KINDS[kind]
    return said.made if preview is not None and preview.made else said.held


def change_status(
    kind: Annotated[str, Describe("A row's kind of change (`ChangeRow.change`): added, changed or removed.")],
    preview: Annotated[Any, Describe("The `ChangePreview` the row is of, which says whether it was made.")] = None,
) -> SafeString:
    """A row's kind of change as the status its Status column says (its Part's about)."""

    if kind not in _KINDS:
        return SafeString("")
    return status_pill(_words(kind, preview), tone=_KINDS[kind].tone)


slotted(
    Part(
        name="change_status",
        about=(
            "A row's kind of change as the status its Status column says, each kind in its own tone: what it "
            "did once made, else what approving it would do."
        ),
        function=change_status,
        example='{% change_status "added" %} {% change_status "changed" %} {% change_status "removed" %}',
    ),
    library=register,
)


def _head(group: ChangeGroup, preview: ChangePreview) -> ChangeRow:
    """The one row of a record that changes: its name, and for one that comes
    or goes whole what it is (its summary) on the side it is on."""

    if not group.whole:
        return ChangeRow(label=group.label, before="", after="", change="changed")
    words = group.summary.after if group.summary is not None else _words(group.whole, preview)
    added = group.whole == "added"
    return ChangeRow(label=group.label, before="" if added else words, after=words if added else "", change=group.whole)


def change_table(preview: ChangePreview) -> dict[str, Any]:
    """The table of ``preview``: what belongs to no record, then each record
    that changes, one row with its fields folded under it by the id its fold
    is named by. A record that comes or goes ``whole`` says its change on its
    own row alone. A record the change lets do more is marked for attention."""

    bodies = (
        [{"head": None, "rows": preview.rows, "folds": "", "whole": "", "attention": False}] if preview.rows else []
    )
    for group in preview.groups:
        if group.changed:
            folds = f"change-{slugify(preview.label)}-{slugify(group.label)}"
            bodies.append(
                {
                    "head": _head(group, preview),
                    "rows": group.rows,
                    "folds": folds,
                    "whole": group.whole,
                    "attention": group.widens,
                }
            )
    return {"preview": preview, "bodies": bodies, "folds": any(body["folds"] for body in bodies)}


slotted(
    Part(
        name="change_table",
        about="What a change moves, each value before and after it, from an `approval_review.ChangePreview`.",
        rules="""
        A change's before and after is `{% change_table preview %}`: a section named by its label. Each
        value that moves stands under Field, Before and After, which share the room evenly
        (`table_region ... compare=True`), each row's kind of change under Status, or under Field and
        Value where nothing stood before. A redacted value is said only to have changed, and a document
        that would not parse is shown as its line diff. A comparison of many records of one kind (the
        adopted playbooks, from `approval_review.grouped`) names each field a record moves by the
        record and the field ("Empty a cache › Access"), says a record that comes or goes whole on one
        row (its name and its summary, its fields folded under it), and names the ones that stay on one
        line under the table. A record a held request's warning names has its rows marked for attention
        (`data-attention` on the `tr`), and the card does not say the warning again. The rows keep the
        change's own order. An empty preview draws nothing. An approval card and an audit event draw it
        (*Gate:* the `change-table` shape of `hand_drawn.SHAPES`).
        """,
        partial="partials/_change_table.html",
        function=change_table,
        example=tags.CHANGE_TABLE,
        states=tags.CHANGE_TABLE_STATES,
    ),
    library=register,
)
