"""Make a new database a named data set (``hq.platform.core.seeds``), owned by an operator it signs up.

    python manage.py seed showcase --owner operator [--env-file seed.env]

Migrating, signing up and seeding in one process starts a scratch server on
one Django boot rather than three. The owner's password is
``DJANGO_SUPERUSER_PASSWORD``, which ``createsuperuser`` reads, or none.
Merging example rows into real ones would leave a database that is neither,
so one that already holds a record is refused and left as it was.
"""

from pathlib import Path
from typing import override

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from hq.platform.core import seeds

# Records are what HQ's domains keep. Users, sessions and the platform's own
# bookkeeping are not.
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
    help = "Make a new database a named data set, owned by an operator it signs up."

    @override
    def add_arguments(self, parser):
        parser.add_argument("name", choices=list(seeds.SEEDS))
        parser.add_argument("--owner", required=True, help="The superuser it signs up to own the set.")
        parser.add_argument("--env-file", type=Path, help="Where to write the settings the set is shown under.")

    @override
    def handle(self, *args, **options):
        seed, owner = seeds.get(options["name"]), options["owner"]
        call_command("migrate", interactive=False, verbosity=0)
        if held := records_held():
            raise CommandError(
                f"This database already holds records, so nothing was seeded: {_counted(held)}. "
                "seed fills an empty database and never merges: point SEVERINO_DATABASE_PATH at a new file."
            )
        with transaction.atomic():
            call_command(
                "createsuperuser", interactive=False, username=owner, email=f"{owner}@example.invalid", verbosity=0
            )
            seed.plant(get_user_model().objects.get_by_natural_key(owner))
        if options["env_file"]:
            options["env_file"].write_text("".join(f"{name}={value}\n" for name, value in seed.settings.items()))
        if options["verbosity"]:
            self.stdout.write(self.style.SUCCESS(f"Seeded {options['name']}: {_counted(records_held())}."))
