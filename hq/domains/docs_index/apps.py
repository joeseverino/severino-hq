from typing import override

from django.apps import AppConfig


class DocsIndexConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hq.domains.docs_index"
    label = "docs_index"
    verbose_name = "Documentation Index"

    @override
    def ready(self):
        from hq.platform.core.audit import register_audit

        from .models import DocumentationRecord

        register_audit(DocumentationRecord, "DocumentationRecord")
