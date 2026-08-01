import json

from django.core.management.base import BaseCommand

from application.search import search_records
from search_index.registry import BY_SCOPE


class Command(BaseCommand):
    help = "Search one HQ record scope through the canonical application service."

    def add_arguments(self, parser):
        parser.add_argument("scope", choices=sorted(BY_SCOPE))
        parser.add_argument("query")
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        self.stdout.write(
            json.dumps(
                search_records(
                    options["scope"],
                    options["query"],
                    limit=options["limit"],
                ),
                indent=2,
            )
        )
