from django.apps import AppConfig


class ContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "content"
    verbose_name = "Content Pipeline"

    def ready(self):
        from core.audit import register_audit
        from .models import ContentItem

        register_audit(ContentItem, "ContentItem")
