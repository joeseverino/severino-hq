from django.apps import AppConfig


class HQAPIConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hq_api"
    verbose_name = "HQ machine API"

    def ready(self):
        # Register the composed capability-contract check after every model has
        # loaded. ``migrate`` and deployment checks then reject a bad extension
        # before the server accepts its first request.
        from . import checks  # noqa: F401
