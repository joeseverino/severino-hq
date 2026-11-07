"""A record's story: every audit row about it, oldest first.

The one read the audit page's record filter and a record's own page share.
A row names the record it is about in its ``subject`` (``register_audit``),
which only rows written since that column exist carry; the log is append-only,
so an older row is never given one and is found by what it does hold: the
record's own rows by object type and id, its operations' and its approvals'
the same way, and the rows of each operation those ids name.
"""

from django.db.models import Q, QuerySet

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource, OperationRequest
from hq.platform.core.audit import audit_events, audited_labels
from hq.platform.core.models import AuditLog


def record_timeline(key: str) -> QuerySet[AuditLog]:
    """Every audit row about the record ``key``, in the order they were written."""

    labels = audited_labels()
    about = Q(subject=key)
    approvals = [str(pk) for pk in ApprovalRequest.objects.filter(resource_key=key).values_list("pk", flat=True)]
    operations: list[str] = []
    resource = ManagedResource.objects.filter(key=key).values_list("pk", flat=True).first()
    if resource is not None:
        about |= Q(object_type=labels[ManagedResource], object_id=str(resource))
        operations = [
            str(pk) for pk in OperationRequest.objects.filter(resource_id=resource).values_list("pk", flat=True)
        ]
        about |= Q(object_type=labels[OperationRequest], object_id__in=operations)
    about |= Q(object_type=labels[ApprovalRequest], object_id__in=approvals)
    # An approval's rows and a queued request's controller work each run under
    # the record's own id.
    about |= Q(operation_id__in=[*approvals, *operations])
    return audit_events().filter(about).order_by("created_at", "pk")
