"""Make a new database a named data set (``hq.platform.core.seeds``), owned by an operator it signs up.

    python manage.py seed showcase --owner operator [--env-file seed.env]

It migrates the database, signs up ``--owner`` as a superuser (with the
password in ``DJANGO_SUPERUSER_PASSWORD``, as ``createsuperuser`` reads it, or
none), and writes the set, in one process, so a scratch server starts on one
Django boot rather than three. ``--env-file`` gets the settings the set is
shown under, as ``NAME=value`` lines for the server's environment.

It writes into an empty estate only. Merging example rows into real ones would
leave a database that is neither, so a database that already holds a record
is refused and left as it was.
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
        parser.add_argument("name", choices=list(seeds.SEEDS), help="The data set.")
        parser.add_argument("--owner", required=True, help="The username of the superuser it signs up to own it.")
        parser.add_argument("--env-file", type=Path, help="Where to write the settings the set is shown under.")

    @override
    def handle(self, *args, **options):
        seed = seeds.get(options["name"])
        call_command("migrate", interactive=False, verbosity=0)
        if held := records_held():
            raise CommandError(
                "This database already holds records, so nothing was seeded: "
                f"{_counted(held)}. seed fills an empty database and never merges. "
                "Point SEVERINO_DATABASE_PATH at a new file and run it there."
            )
        with transaction.atomic():
            call_command(
                "createsuperuser",
                interactive=False,
                username=options["owner"],
                email=f"{options['owner']}@example.invalid",
                verbosity=0,
            )
            seed.plant(get_user_model()._default_manager.get_by_natural_key(options["owner"]))
        if options["env_file"]:
            options["env_file"].write_text("".join(f"{name}={value}\n" for name, value in seed.settings.items()))
        if options["verbosity"]:
            self.stdout.write(
                self.style.SUCCESS(f"Seeded {options['name']}: {_counted(records_held()) or 'no records'}.")
            )
