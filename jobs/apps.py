from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "jobs"
    verbose_name = "Background jobs"

    def ready(self):
        from core.audit import register_audit

        from .models import Job

        # Registered like every other record HQ keeps, so starting a job that
        # reads an operator's archive leaves the same trail as editing a row
        # by hand. Work that happens off the request thread is exactly the
        # work an audit log is for -- nobody watched it happen.
        register_audit(Job, "Background job")
