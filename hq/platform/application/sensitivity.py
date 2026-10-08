"""Which documentation an AI-facing surface may name, stated once.

MCP and API results, search and the relationship lists every record carries
all ask the same question of a documentation record. Answered in five places,
a sensitivity added to one of them would quietly widen one surface and not
the others.

The set is defined beside the model's sensitivity choices, which it is made
of, and the model's own ``is_safe_for_ai_export`` reads it there. This module
is how every other surface asks: it imports the model, never the reverse, so
persistence does not depend on the application layer.
"""

from django.db.models import QuerySet

from hq.domains.docs_index.models import SAFE_SENSITIVITIES, DocumentationRecord

__all__ = ["SAFE_SENSITIVITIES", "safe_doc_ids", "safe_records"]


def safe_doc_ids(documentation: QuerySet[DocumentationRecord]) -> list[str]:
    """The ``doc_id`` of each related record an AI-facing surface may name, in order."""

    return list(
        documentation.filter(sensitivity__in=SAFE_SENSITIVITIES).order_by("doc_id").values_list("doc_id", flat=True)
    )


def safe_records() -> QuerySet[DocumentationRecord]:
    """Every documentation record an AI-facing surface may name."""

    return DocumentationRecord.objects.filter(sensitivity__in=SAFE_SENSITIVITIES)
