from typing import override

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hq.platform.core"
    label = "core"

    @override
    def ready(self):
        from django.db.models.signals import post_migrate

        from . import checks, signals  # noqa: F401 - imported to register checks and receivers
        from .outbound import install as hold_requests
        from .print_gate import install as gate_prints

        # From here on a request cannot wait on anything outside the process.
        hold_requests()
        # And a page prints only text and numbers.
        gate_prints()
        from .revisions import after_migrate

        # Sent after the whole migrate, so every app's tables exist.
        post_migrate.connect(after_migrate, sender=self, dispatch_uid="hq.revisions")
