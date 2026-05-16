from django.apps import AppConfig


class DocsIndexConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "docs_index"
    verbose_name = "Documentation Index"

    def ready(self):
        from core.audit import register_audit
        from .models import DocumentationRecord

        register_audit(DocumentationRecord, "DocumentationRecord")
