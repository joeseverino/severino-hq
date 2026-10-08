import json
from typing import override

from django.core.management.base import BaseCommand

from hq.platform.application.search import search_records
from hq.platform.application.security import cli_principal
from hq.platform.search_index.registry import BY_SCOPE


class Command(BaseCommand):
    help = "Search one HQ record scope through the canonical application service."

    @override
    def add_arguments(self, parser):
        parser.add_argument("scope", choices=sorted(BY_SCOPE))
        parser.add_argument("query")
        parser.add_argument("--limit", type=int, default=50)

    @override
    def handle(self, *args, **options):
        self.stdout.write(
            json.dumps(
                search_records(
                    options["scope"],
                    options["query"],
                    principal=cli_principal(),
                    limit=options["limit"],
                ),
                indent=2,
            )
        )
