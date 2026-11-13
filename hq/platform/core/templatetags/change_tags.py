"""The change table: what a change moves, each value before and after it.

``{% change_table preview %}`` draws ``partials/_change_table.html`` from an
``approval_review.ChangePreview``: a section named by its label, the rows
under Field, Before and After (Field and Value where nothing stood before)
in the change's own order, each changed record of many in rows named by it,
the records that stay named on one line under it, and the line diff of what
would not parse. An empty preview draws nothing.
"""

from typing import Any

from django import template
from django.utils.text import slugify

from hq.platform.application.payload_words import ChangeRow

register = template.Library()

# What a record that comes or goes whole says on its row when it has no summary.
_WHOLE_WORDS = {"added": "New", "removed": "Removed"}


def _head(group: Any) -> ChangeRow:
    """The one row of a record that comes or goes whole: its name, and what
    it is (its summary) on the side it is on."""

    words = group.summary.after if group.summary is not None else _WHOLE_WORDS[group.whole]
    added = group.whole == "added"
    return ChangeRow(label=group.label, before="" if added else words, after=words if added else "", change=group.whole)


@register.inclusion_tag("partials/_change_table.html")
def change_table(preview: Any) -> dict[str, Any]:
    """The table of ``preview``: what belongs to no record, then each record
    that changes. A record that moves is a row for each field, named by the
    record and the field; one that comes or goes whole is one row, its fields
    folded under it by the id its fold is named by. A record the change lets
    do more is marked for attention."""

    bodies = [{"head": None, "rows": preview.rows, "folds": "", "attention": False}] if preview.rows else []
    for group in preview.groups:
        if not group.changed:
            continue
        if group.whole:
            folds = f"change-{slugify(preview.label)}-{slugify(group.label)}"
            bodies.append({"head": _head(group), "rows": group.rows, "folds": folds, "attention": group.widens})
        else:
            bodies.append({"head": None, "rows": group.named, "folds": "", "attention": group.widens})
    return {"preview": preview, "bodies": bodies, "folds": any(body["folds"] for body in bodies)}
