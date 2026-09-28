"""Which documentation an AI-facing surface may name, stated once.

MCP and API results, search and the relationship lists every record carries
all ask the same question of a documentation record. Answered in five places,
a sensitivity added to one of them would quietly widen one surface and not
the others.
"""

from __future__ import annotations

from django.db.models import QuerySet

from docs_index.models import AI_SAFE_SENSITIVITIES, DocumentationRecord

# The model owns which sensitivities are safe; every surface filters with this.
SAFE_SENSITIVITIES = AI_SAFE_SENSITIVITIES


def safe_doc_ids(documentation: QuerySet[DocumentationRecord]) -> list[str]:
    """The ``doc_id`` of each related record an AI-facing surface may name, in order."""

    return list(
        documentation.filter(sensitivity__in=SAFE_SENSITIVITIES)
        .order_by("doc_id")
        .values_list("doc_id", flat=True)
    )
