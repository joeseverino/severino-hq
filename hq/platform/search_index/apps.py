from typing import override

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def reproject(sender, plan=None, **kwargs):
    """A migration of the index changes what its rows hold, so they are drawn
    again from their records once every app is migrated, as the records then
    stand: a migration's own models are the schema of its moment."""

    if any(migration.app_label == sender.label and not backwards for migration, backwards in plan or ()):
        # Imported here: this module loads before any model may.
        from .services import rebuild_search_index

        rebuild_search_index()


class SearchIndexConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hq.platform.search_index"
    label = "search_index"

    @override
    def ready(self):
        from . import signals  # noqa: F401 - imported to register its receivers

        post_migrate.connect(reproject, sender=self, dispatch_uid="hq.search_index.reproject")
