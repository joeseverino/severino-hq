from typing import override

from django.apps import AppConfig


class AssetsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'hq.domains.assets'
    label = 'assets'
    verbose_name = "Assets & Equipment"

    @override
    def ready(self):
        from hq.platform.core.audit import register_audit

        from .models import Asset

        register_audit(Asset, "Asset")
