from django.apps import AppConfig


class ControlPlaneConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "control_plane"
    verbose_name = "Infrastructure Control Plane"

    def ready(self):
        from core.audit import register_audit

        from .models import ManagedResource, OperationRequest

        register_audit(ManagedResource, "Managed resource")
        register_audit(OperationRequest, "Infrastructure operation")
