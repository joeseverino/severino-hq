"""The audit log, as a searchable resource."""

from hq.platform.core.models import AuditLog

from .integration_specs import ResourceSpec
from .search_contracts import SearchDefinition
from .security import Capability


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "audit",
            "Audit log",
            "Security-sensitive audit events indexed by HQ.",
            Capability.READ_AUDIT_LOG,
            search=SearchDefinition(
                "audit",
                AuditLog,
                "pk",
                ("action", "object_type", "object_id", "object_repr", "operation_id", "message"),
                label="Audit log",
                title_field="search_title",
                badge_field="type_label",
                timestamp_field="created_at",
                snippet_field="search_snippet",
            ),
            web_route="core:audit_list",
        ),
    )
