from pathlib import Path
from typing import override

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

lazy from hq.platform.application.connection_setup import reference_markdown


def reference_path() -> Path:
    return Path(settings.BASE_DIR) / "docs" / "Connections.md"


class Command(BaseCommand):
    help = "Write docs/Connections.md from the providers' declarations, or --check it for drift."

    @override
    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Exit 1 when docs/Connections.md is behind.")

    @override
    def handle(self, *args, **options):
        current = reference_markdown()
        path = reference_path()
        if options["check"]:
            if not path.exists() or path.read_text(encoding="utf-8") != current:
                raise CommandError("docs/Connections.md is behind the providers; run `mise run fix`.")
            self.stdout.write("docs/Connections.md is current.")
            return
        path.write_text(current, encoding="utf-8")
        self.stdout.write(f"Wrote {path.name}.")
