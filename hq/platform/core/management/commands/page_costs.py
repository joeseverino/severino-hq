"""What each page costs to draw on this database, as one line of JSON (hq.platform.core.page_costs).

    python manage.py page_costs [<address> ...]

Without an address it walks every page the navigation and the header list.
"""

import json
from typing import override

from django.core.management.base import BaseCommand, CommandParser

from hq.platform.core import page_costs


class Command(BaseCommand):
    help = "Print each page's queries, records and HTML bytes, rendered in this process, as one line of JSON."

    @override
    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("pages", nargs="*", help="The addresses to walk; every listed page when none.")

    @override
    def handle(self, *args, **options):
        self.stdout.write(json.dumps(page_costs.walk(options["pages"] or page_costs.listed_pages())))
