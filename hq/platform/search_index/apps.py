from django.apps import AppConfig


class SearchIndexConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'hq.platform.search_index'
    label = 'search_index'

    def ready(self):
        from . import signals  # noqa: F401
