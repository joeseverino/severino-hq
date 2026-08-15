from django.core.management.base import BaseCommand

from application.plugins import describe_plugins


class Command(BaseCommand):
    help = "Emit the validated, explicitly enabled HQ plugin inventory."

    def handle(self, *args, **options):
        import json

        self.stdout.write(json.dumps(describe_plugins(), indent=2, sort_keys=True))
