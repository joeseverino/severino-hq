"""The change table: what a change moves, each value before and after it.

``{% change_table preview %}`` draws ``partials/_change_table.html`` from an
``approval_review.ChangePreview``: a section named by its label, the rows
under Field, Before and After in the change's own order, each row's kind of
change under Status (Field and Value alone where nothing stood before), each
changed record of many one row with its fields folded under it, the records
that stay named on one line under it, and the line diff of what would not
parse. An empty preview draws nothing.
"""

from typing import Any, NamedTuple

from django import template
from django.utils.safestring import SafeString
from django.utils.text import slugify

from hq.platform.application.payload_words import ChangeRow
from hq.platform.application.tone import Tone
from hq.platform.core.templatetags.status_tags import status_pill
lazy from hq.platform.application.approval_review import ChangeGroup, ChangePreview

register = template.Library()


class _Kind(NamedTuple):
    made: str
    held: str
    tone: Tone


# What a row's kind of change is called: as a change made says it, and as one
# held for approval says what it would do. Its tone: what comes is good news,
# what moves asks attention, what goes is serious. A record that comes or goes
# whole with no summary of its own says the same on its row.
_KINDS = {
    "added": _Kind("New", "Would add", Tone.GOOD),
    "changed": _Kind("Changed", "Would change", Tone.ATTENTION),
    "removed": _Kind("Removed", "Would remove", Tone.SERIOUS),
}


def _words(kind: str, preview: ChangePreview | None) -> str:
    """What ``kind`` is called in ``preview``: what it did once made, else
    what approving it would do, as a table of no preview is held too."""

    said = _KINDS[kind]
    return said.made if preview is not None and preview.made else said.held


@register.simple_tag
def change_status(kind: str, preview: ChangePreview | None = None) -> SafeString:
    """A row's kind of change (``ChangeRow.change``) as the status its
    Status column says in ``preview``, each kind in its own tone."""

    if kind not in _KINDS:
        return SafeString("")
    return status_pill(_words(kind, preview), tone=_KINDS[kind].tone)


def _head(group: ChangeGroup, preview: ChangePreview) -> ChangeRow:
    """The one row of a record that changes: its name, and for one that comes
    or goes whole what it is (its summary) on the side it is on."""

    if not group.whole:
        return ChangeRow(label=group.label, before="", after="", change="changed")
    words = group.summary.after if group.summary is not None else _words(group.whole, preview)
    added = group.whole == "added"
    return ChangeRow(label=group.label, before="" if added else words, after=words if added else "", change=group.whole)


@register.inclusion_tag("partials/_change_table.html")
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
