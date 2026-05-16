from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "projects"
    verbose_name = "Projects & Labs"

    def ready(self):
        from core.audit import register_audit
        from .models import Project

        register_audit(Project, "Project")
