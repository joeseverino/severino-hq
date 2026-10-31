"""Which documentation an AI-facing surface may name, stated once.

MCP and API results, search and every record's relationship lists ask the same
question of a documentation record; answered in each, a sensitivity added to one
would quietly widen one surface and not the others. The set lives beside the
model's sensitivity choices it is made of, and this module imports it, never
the reverse, so persistence does not depend on the application layer.
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
