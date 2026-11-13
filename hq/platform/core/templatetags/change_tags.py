"""The change table: what a change moves, each value before and after it.

``{% change_table preview %}`` draws ``partials/_change_table.html`` from an
``approval_review.ChangePreview``: a section named by its label, the rows
under Field, Before and After (Field and Value where nothing stood before)
in the change's own order, each changed record of many a group of its own,
the records that stay named on one line under it, and the line diff of what
would not parse. An empty preview draws nothing.
"""

from typing import Any

from django import template
from django.utils.text import slugify

register = template.Library()


@register.inclusion_tag("partials/_change_table.html")
def change_table(preview: Any) -> dict[str, Any]:
    """The table of ``preview``: what belongs to no record, then each record
    that changes, with the id its fold is named by when it comes or goes
    whole, and whether a warning names it."""

    bodies = [
        {"label": "", "said": "", "rows": preview.rows, "folds": "", "summary": None, "whole": "", "attention": False}
    ]
    bodies = bodies if preview.rows else []
    # A record that comes or goes whole is said by its summary, its fields folded under it.
    bodies += [
        {
            "label": group.label,
            "said": group.said,
            "rows": group.rows,
            "folds": f"change-{slugify(preview.label)}-{slugify(group.label)}" if group.whole else "",
            "summary": group.summary if group.whole else None,
            "whole": group.whole,
            "attention": bool(group.warning),
        }
        for group in preview.groups
        if group.changed
    ]
    return {"preview": preview, "bodies": bodies, "folds": any(body["folds"] for body in bodies)}
