"""The change table: what a change moves, each value before and after it.

``{% change_table preview %}`` draws ``partials/_change_table.html`` from an
``approval_review.ChangePreview``: a section named by its label, the rows
under Field, Before and After (Field and Value where nothing stood before)
in the change's own order, each row's kind of change under Status, each
changed record of many one row with its fields folded under it, the records
that stay named on one line under it, and the line diff of what would not
parse. An empty preview draws nothing.
"""

from typing import Any

from django import template
from django.utils.safestring import SafeString
from django.utils.text import slugify

from hq.platform.application.payload_words import ChangeRow
from hq.platform.core.templatetags.status_tags import status_pill

register = template.Library()

# What a record that comes or goes whole says on its row when it has no summary.
_WHOLE_WORDS = {"added": "New", "removed": "Removed"}

# What a row's kind of change is called in its Status column, and its tone:
# what comes is good news, what moves asks attention, what goes is serious.
_KINDS = {"added": ("New", "good"), "changed": ("Changed", "attention"), "removed": ("Removed", "serious")}


@register.simple_tag
def change_status(kind: str) -> SafeString:
    """A row's kind of change (``ChangeRow.change``) as the status its
    Status column says, each kind in its own tone."""

    word, tone = _KINDS.get(kind, ("", ""))
    return status_pill(word, tone=tone) if word else SafeString("")


def _head(group: Any) -> ChangeRow:
    """The one row of a record that changes: its name, and for one that comes
    or goes whole what it is (its summary) on the side it is on."""

    if not group.whole:
        return ChangeRow(label=group.label, before="", after="", change="changed")
    words = group.summary.after if group.summary is not None else _WHOLE_WORDS[group.whole]
    added = group.whole == "added"
    return ChangeRow(label=group.label, before="" if added else words, after=words if added else "", change=group.whole)


@register.inclusion_tag("partials/_change_table.html")
def change_table(preview: Any) -> dict[str, Any]:
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
                    "head": _head(group),
                    "rows": group.rows,
                    "folds": folds,
                    "whole": group.whole,
                    "attention": group.widens,
                }
            )
    return {"preview": preview, "bodies": bodies, "folds": any(body["folds"] for body in bodies)}
