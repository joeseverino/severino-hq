"""Fill an empty database with the example estate, the homelab the public demo shows.

    python manage.py seed_demo

It writes into an empty estate only. Merging example rows into real ones would
leave a database that is neither, so a database that already holds a record
is refused and left as it was.
"""

from typing import override

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from hq.platform.core import example_estate

# Records are what HQ's domains keep. Users, sessions and the platform's own
# bookkeeping are not: an operator signs up before seeding.
_DOMAINS = "hq.domains."


def records_held() -> dict[str, int]:
    """Each domain model that holds rows, by its label, with how many."""

    held = {}
    for model in apps.get_models():
        if model.__module__.startswith(_DOMAINS) and not model._meta.proxy:
            count = model._default_manager.count()
            if count:
                held[model._meta.label] = count
    return held


def _counted(held: dict[str, int]) -> str:
    return ", ".join(f"{label} ({count})" for label, count in sorted(held.items()))


class Command(BaseCommand):
    help = "Fill an empty database with the example homelab the demo shows."

    @override
    def handle(self, *args, **options):
        if held := records_held():
            raise CommandError(
                "This database already holds records, so nothing was seeded: "
                f"{_counted(held)}. seed_demo fills an empty database and never merges. "
                "Point SEVERINO_DATABASE_PATH at a new file, migrate it, and run it there."
            )
        with transaction.atomic():
            example_estate.seed()
        if options["verbosity"]:
            self.stdout.write(self.style.SUCCESS(f"Seeded the example estate: {_counted(records_held())}."))
