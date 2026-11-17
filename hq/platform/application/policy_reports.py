"""A Content Security Policy violation a browser reported, kept in the audit log.

No capability: the report arrives without credentials, from whichever browser
was refused, so there is nobody to authorize and nothing to retry under a key.
It is bounded instead (``CSP_REPORTS``), and its endpoint answers the same
whatever is decided here.
"""

from datetime import timedelta

from django.utils import timezone

from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog
from hq.platform.core.write_bounds import RowCap

lazy from .ui import ended

_LABEL = "ContentSecurityPolicy"
# One row per distinct complaint in this long. A page that violates the policy
# on every load would otherwise write a row on every load, and the thousandth
# copy says nothing the first did not.
_REPEAT_WINDOW = timedelta(hours=1)

CSP_REPORTS = RowCap(
    "policy reports",
    "at most 60 rows an hour, of a directive HQ's policy governs and a blocked origin or keyword",
    action=AuditLog.Action.FAILED,
    type_label=_LABEL,
    rows=60,
    window=timedelta(hours=1),
)


def record_violation(*, directive: str, blocked: str, document: str, source: str) -> None:
    """Keep one violation of ``directive``, unless the same one is already kept or the bound is spent."""

    already = AuditLog.objects.filter(
        action=AuditLog.Action.FAILED,
        object_type=_LABEL,
        metadata__directive=directive,
        metadata__blocked=blocked,
        created_at__gte=timezone.now() - _REPEAT_WINDOW,
    ).exists()
    if already or not CSP_REPORTS.admits():
        return
    record_event(
        action=AuditLog.Action.FAILED,
        type_label=_LABEL,
        message=ended(f"The browser refused {blocked or 'a resource'} under {directive}"),
        metadata={"directive": directive, "blocked": blocked, "document": document, "source": source},
    )
