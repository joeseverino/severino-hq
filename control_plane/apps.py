from django.apps import AppConfig


class ControlPlaneConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "control_plane"
    verbose_name = "Infrastructure Control Plane"

    def ready(self):
        from application.approvals import AUDIT_LABEL as APPROVAL_AUDIT_LABEL
        from core.audit import register_audit

        from .models import ApprovalRequest, ManagedResource, OperationRequest

        register_audit(
            ManagedResource,
            "Managed resource",
            # A sweep stamps this on every declaration it confirms. That is HQ
            # reporting that it looked, not the world reporting that it moved.
            observation=("last_observed_at",),
        )
        register_audit(OperationRequest, "Infrastructure operation")
        # Audited like everything else, and for the reason the whole feature
        # exists: who asked for a held change, who agreed to it and when are
        # exactly the facts an incident review has to be able to read back.
        register_audit(ApprovalRequest, APPROVAL_AUDIT_LABEL)
