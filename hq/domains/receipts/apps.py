from django.apps import AppConfig


class ReceiptsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'hq.domains.receipts'
    label = 'receipts'
    verbose_name = "Receipts"

    def ready(self):
        from hq.platform.core.audit import register_audit

        from .models import Receipt

        register_audit(Receipt, "Receipt")
