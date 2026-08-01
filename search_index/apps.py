from django.apps import AppConfig


class SearchIndexConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "search_index"

    def ready(self):
        from . import signals  # noqa: F401
