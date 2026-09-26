from django.apps import AppConfig


class ControlPlaneConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "control_plane"
    verbose_name = "Infrastructure Control Plane"

    def ready(self):
        from application.approvals import AUDIT_LABEL as APPROVAL_AUDIT_LABEL
        from core.audit import register_audit

        from .models import ApprovalRequest, ManagedResource, NotManaged, OperationRequest

        register_audit(
            ManagedResource,
            "Managed resource",
            # A sweep stamps this on every declaration it confirms. That is HQ
            # reporting that it looked, not the world reporting that it moved.
            observation=("last_observed_at",),
            connection=_resource_connection,
        )
        register_audit(
            OperationRequest,
            "Infrastructure operation",
            connection=lambda operation: _resource_connection(operation.resource),
        )
        # An operator's choice that a record stays unmanaged, and its reversal.
        register_audit(NotManaged, "Not managed")
        # Who asked for a held change, who agreed to it and when.
        register_audit(ApprovalRequest, APPROVAL_AUDIT_LABEL)


def _resource_connection(resource) -> str:
    """The connection a resource's work goes through, from its spec."""

    spec = resource.spec if isinstance(resource.spec, dict) else {}
    return str(spec.get("connection_ref", "") or "")
