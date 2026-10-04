from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'hq.platform.core'
    label = 'core'

    def ready(self):
        from . import checks, signals  # noqa: F401
