"""The change table: what a change moves, each value before and after it.

``{% change_table preview %}`` draws ``partials/_change_table.html`` from an
``approval_review.ChangePreview``: a section named by its label, the rows
under Field, Before and After (Field and Value where nothing stood before),
each record of many a group of its own, folded when it does not change, and
the line diff of what would not parse. An empty preview draws nothing.
"""

from typing import Any

from django import template
from django.utils.text import slugify

register = template.Library()


@register.inclusion_tag("partials/_change_table.html")
def change_table(preview: Any) -> dict[str, Any]:
    """The table of ``preview``: what belongs to no record, then each record,
    with the id its fold is named by when it starts folded."""

    bodies = [{"label": "", "said": "", "rows": preview.rows, "folds": ""}] if preview.rows else []
    bodies += [
        {
            "label": group.label,
            "said": group.said,
            "rows": group.rows,
            "folds": "" if group.changed else f"change-{slugify(preview.label)}-{slugify(group.label)}",
        }
        for group in preview.groups
    ]
    return {"preview": preview, "bodies": bodies, "folds": any(body["folds"] for body in bodies)}
