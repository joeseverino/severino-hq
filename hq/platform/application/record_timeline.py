"""A record's story: every audit row about it, oldest first.

The one read the audit page's record filter and a record's own page share. The
log is append-only, so a row from before ``subject`` existed is found by its
object type and id.
"""

from django.db.models import Q, QuerySet

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource, OperationRequest
from hq.platform.core.audit import audit_events, audited_labels
from hq.platform.core.models import AuditLog


def record_timeline(*keys: str) -> QuerySet[AuditLog]:
    """Every audit row about the records ``keys`` name, in the order they were written."""

    labels = audited_labels()
    approvals = [str(pk) for pk in ApprovalRequest.objects.filter(resource_key__in=keys).values_list("pk", flat=True)]
    resources = [str(pk) for pk in ManagedResource.objects.filter(key__in=keys).values_list("pk", flat=True)]
    operations = [
        str(pk) for pk in OperationRequest.objects.filter(resource__key__in=keys).values_list("pk", flat=True)
    ]
    about = (
        Q(subject__in=keys)
        | Q(object_type=labels[ManagedResource], object_id__in=resources)
        | Q(object_type=labels[OperationRequest], object_id__in=operations)
        | Q(object_type=labels[ApprovalRequest], object_id__in=approvals)
        # An approval's rows and a queued request's controller work each run
        # under the record's own id.
        | Q(operation_id__in=[*approvals, *operations])
    )
    return audit_events().filter(about).order_by("created_at", "pk")
