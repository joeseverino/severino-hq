"""Read what is due from the public registries and store it."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from application.public_registry import refresh
from application.security import cli_principal


class Command(BaseCommand):
    help = "Read public registry records that are due and store them as readings."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Ignore the hourly throttle.")

    def handle(self, *args, **options):
        result = refresh(principal=cli_principal(), force=options["force"])
        self.stdout.write(json.dumps(result, default=str, sort_keys=True))
