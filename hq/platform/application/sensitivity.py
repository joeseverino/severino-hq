"""Which documentation an AI-facing surface may name, stated once, and who
reads past it: a principal holding ``read_sensitive_documentation``, which
only a person can (``security.HELD_ONLY_AT``).

MCP and API results, search and every record's relationship lists ask the same
question of a documentation record; answered in each, a sensitivity added to one
would quietly widen one surface and not the others. The set lives beside the
model's sensitivity choices it is made of, and this module imports it, never
the reverse, so persistence does not depend on the application layer.
"""

from django.db.models import QuerySet

from hq.domains.docs_index.models import AI_SAFE, DocumentationRecord

from .security import Capability, Principal

__all__ = ["readable_records", "reads_whole", "safe_doc_ids", "safe_records", "shared_doc"]


def safe_doc_ids(documentation: QuerySet[DocumentationRecord]) -> list[str]:
    """The ``doc_id`` of each related record an AI-facing surface may name, in order."""

    return list(documentation.filter(AI_SAFE).order_by("doc_id").values_list("doc_id", flat=True))


def safe_records() -> QuerySet[DocumentationRecord]:
    """Every documentation record an AI-facing surface may name."""

    return DocumentationRecord.objects.filter(AI_SAFE)


def reads_whole(principal: Principal | None) -> bool:
    """Whether ``principal`` reads every doc whole; None is an AI-facing surface's reader."""

    return principal is not None and principal.permits(Capability.READ_SENSITIVE_DOCUMENTATION)


def readable_records(principal: Principal | None) -> QuerySet[DocumentationRecord]:
    """Every documentation record ``principal`` may read."""

    return DocumentationRecord.objects.all() if reads_whole(principal) else safe_records()


def shared_doc(record: DocumentationRecord | None, principal: Principal | None) -> DocumentationRecord | None:
    """``record``, the doc another record links to, when ``principal`` may read it; else None."""

    if record is None or not (record.is_safe_for_ai_export or reads_whole(principal)):
        return None
    return record
