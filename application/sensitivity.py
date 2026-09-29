"""Which documentation an AI-facing surface may name, stated once.

MCP and API results, search and the relationship lists every record carries
all ask the same question of a documentation record. Answered in five places,
a sensitivity added to one of them would quietly widen one surface and not
the others.

The record's own ``is_safe_for_ai_export`` reads this set too, so this module
must not import the model at runtime: the model imports it. The values are the
``DocumentationRecord.Sensitivity`` choices' stored strings (a ``TextChoices``
member equals its value), and ``test_sensitivity`` holds them to those choices.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from docs_index.models import DocumentationRecord


# DocumentationRecord.Sensitivity.PUBLIC and .INTERNAL.
SAFE_SENSITIVITIES: tuple[str, ...] = ("public", "internal")


def safe_doc_ids(documentation: QuerySet[DocumentationRecord]) -> list[str]:
    """The ``doc_id`` of each related record an AI-facing surface may name, in order."""

    return list(
        documentation.filter(sensitivity__in=SAFE_SENSITIVITIES)
        .order_by("doc_id")
        .values_list("doc_id", flat=True)
    )
