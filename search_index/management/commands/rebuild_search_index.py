import json

from django.core.management.base import BaseCommand

from search_index.services import rebuild_search_index


class Command(BaseCommand):
    help = "Rebuild the HQ full-text search projection and FTS5 index."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(rebuild_search_index(), sort_keys=True))
