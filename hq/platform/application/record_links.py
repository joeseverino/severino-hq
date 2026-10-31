"""The relationships a registered record answers with, declared once.

An asset and a project are each the hub of the same web: the documentation
that describes them, the content that mentions them, the expenses that paid
for them, and each other. A fetch answers that web beside the record's own
fields, in one shape, so a client reads either the same way.
"""

from typing import Any

from django.db.models import QuerySet

from .sensitivity import safe_doc_ids


def _slugs(related: QuerySet) -> list[str]:
    return list(related.order_by("slug").values_list("slug", flat=True))


def record_links(row: Any, **slug_sets: QuerySet) -> dict[str, list[Any]]:
    """The documentation, content and expenses ``row`` is linked to, plus each named set of slugs.

    ``row`` has the reverse relations ``documentation_records``,
    ``content_items`` and ``expenses``; the sets a record links that the other
    does not (an asset's projects, a project's assets) are passed by name.
    """

    return {
        **{name: _slugs(related) for name, related in slug_sets.items()},
        "documentation": safe_doc_ids(row.documentation_records),
        "content": _slugs(row.content_items),
        "expense_ids": list(row.expenses.order_by("-date", "-id").values_list("id", flat=True)),
    }


def linked(fields: dict[str, Any], row: Any, *, relationships: bool, **slug_sets: QuerySet) -> dict[str, Any]:
    """A record's ``fields``, with its ``relationships`` when a caller asked for them."""

    if relationships:
        fields["relationships"] = record_links(row, **slug_sets)
    return fields
