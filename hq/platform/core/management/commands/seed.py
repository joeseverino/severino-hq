"""Make a new database a named data set (``hq.platform.core.seeds``), owned by an operator it signs up.

    python manage.py seed showcase --owner operator [--env-file seed.env]

Migrating, signing up and seeding in one process starts a scratch server on
one Django boot rather than three.
"""

from pathlib import Path
from typing import override

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from hq.platform.core import seeds


class Command(BaseCommand):
    help = "Make a new database a named data set, owned by an operator it signs up."

    @override
    def add_arguments(self, parser):
        parser.add_argument("name", choices=list(seeds.SEEDS))
        parser.add_argument("--owner", required=True, help="The superuser it signs up to own the set.")
        parser.add_argument("--env-file", type=Path, help="Where to write the settings the set is shown under.")

    @override
    def handle(self, *args, **options):
        name, owner = options["name"], options["owner"]
        call_command("migrate", interactive=False, verbosity=0)
        try:
            with transaction.atomic():
                # The owner's password is DJANGO_SUPERUSER_PASSWORD, which
                # createsuperuser reads, or none.
                call_command(
                    "createsuperuser", interactive=False, username=owner, email=f"{owner}@example.invalid", verbosity=0
                )
                seeds.plant(name, get_user_model().objects.get_by_natural_key(owner))
        except seeds.SeedRefused as refused:
            raise CommandError(str(refused)) from None
        if options["env_file"]:
            shown_under = seeds.get(name).settings.items()
            options["env_file"].write_text("".join(f"{setting}={value}\n" for setting, value in shown_under))
        if options["verbosity"]:
            self.stdout.write(self.style.SUCCESS(f"Seeded {name}: {seeds.counted(seeds.records_held())}."))
