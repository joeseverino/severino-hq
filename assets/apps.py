from django.apps import AppConfig


class AssetsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "assets"
    verbose_name = "Assets & Equipment"

    def ready(self):
        from core.audit import register_audit
        from .models import Asset

        register_audit(Asset, "Asset")
