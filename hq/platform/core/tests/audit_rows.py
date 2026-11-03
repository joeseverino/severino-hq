"""The audit rows a test's own steps wrote.

The log is append-only, so a test that asks what one step recorded marks
where the log stood before it rather than emptying it.
"""

from django.db.models import Max, QuerySet

from hq.platform.core.models import AuditLog


def mark() -> int:
    """Where the log stands: the newest row's id, or 0."""

    return AuditLog.objects.aggregate(newest=Max("pk"))["newest"] or 0


def since(marked: int) -> QuerySet[AuditLog]:
    """Every row written after ``marked``."""

    return AuditLog.objects.filter(pk__gt=marked)
