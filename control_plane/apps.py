from django.apps import AppConfig


class ControlPlaneConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "control_plane"
    verbose_name = "Infrastructure Control Plane"

    def ready(self):
        from core.audit import register_audit

        from .models import ManagedResource, OperationRequest

        register_audit(
            ManagedResource,
            "Managed resource",
            # A sweep stamps this on every declaration it confirms. That is HQ
            # reporting that it looked, not the world reporting that it moved.
            observation=("last_observed_at",),
        )
        register_audit(OperationRequest, "Infrastructure operation")
