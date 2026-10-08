import difflib
from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Write hq_api/hq-api.openapi.json from the API's routes and registries, or --check it for drift."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit 1 when the committed document differs from the derived one.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from hq.platform.api.openapi import DOCUMENT_PATH, document, render

        current = render(document())
        if options["check"]:
            committed = DOCUMENT_PATH.read_text(encoding="utf-8") if DOCUMENT_PATH.exists() else ""
            if committed != current:
                self.stderr.write(
                    "".join(difflib.unified_diff(
                        committed.splitlines(keepends=True),
                        current.splitlines(keepends=True),
                        f"committed/{DOCUMENT_PATH.name}",
                        f"derived/{DOCUMENT_PATH.name}",
                    )),
                    ending="",
                )
                raise CommandError(
                    f"{DOCUMENT_PATH.name} is behind the API; run `manage.py api_openapi` "
                    "(with no extensions installed) and commit the result."
                )
            self.stdout.write(f"{DOCUMENT_PATH.name} is current.")
            return
        DOCUMENT_PATH.write_text(current, encoding="utf-8")
        self.stdout.write(f"Wrote {DOCUMENT_PATH}.")
