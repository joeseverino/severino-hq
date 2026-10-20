from typing import override
lazy import json

from django.core.management.base import BaseCommand

from hq.platform.application.plugins import describe_plugins


class Command(BaseCommand):
    help = "Emit the validated, explicitly enabled HQ plugin inventory."

    @override
    def handle(self, *args, **options):
        self.stdout.write(json.dumps(describe_plugins(), indent=2, sort_keys=True))
