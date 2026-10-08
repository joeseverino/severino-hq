from typing import override

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'hq.platform.core'
    label = 'core'

    @override
    def ready(self):
        from django.db.models.signals import post_migrate

        from . import checks, signals  # noqa: F401
        from .outbound import install

        # From here on a request cannot wait on anything outside the process.
        install()
        from .revisions import after_migrate

        # Sent after the whole migrate, so every app's tables exist.
        post_migrate.connect(after_migrate, sender=self, dispatch_uid="hq.revisions")
