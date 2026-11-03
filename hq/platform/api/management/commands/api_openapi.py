import difflib
from argparse import ArgumentParser
from typing import Any, override

from django.core.management.base import BaseCommand, CommandError

lazy from hq.platform.api.openapi import DOCUMENT_PATH, EXAMPLES_PATH, document, prune_examples, render


class Command(BaseCommand):
    help = "Write hq_api/hq-api.openapi.json from the API's routes and registries, or --check it for drift."

    @override
    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit 1 when the committed document differs from the derived one.",
        )
        parser.add_argument(
            "--prune-examples",
            action="store_true",
            help=f"Drop each entry of {EXAMPLES_PATH.name} whose operation the API no longer has, and write nothing else.",
        )

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        derived = document()
        if options["prune_examples"]:
            gone = prune_examples(derived)
            self.stdout.write(
                f"Dropped from {EXAMPLES_PATH.name}: {', '.join(gone)}." if gone else "No example to drop."
            )
            return
        current = render(derived)
        if options["check"]:
            committed = DOCUMENT_PATH.read_text(encoding="utf-8") if DOCUMENT_PATH.exists() else ""
            if committed != current:
                self.stderr.write(
                    "".join(
                        difflib.unified_diff(
                            committed.splitlines(keepends=True),
                            current.splitlines(keepends=True),
                            f"committed/{DOCUMENT_PATH.name}",
                            f"derived/{DOCUMENT_PATH.name}",
                        )
                    ),
                    ending="",
                )
                raise CommandError(
                    f"{DOCUMENT_PATH.name} is behind the API; run `mise run manage -- api_openapi` and commit the result, "
                    f"or `mise run fix:api-examples` when a response's shape changed, which records "
                    f"{EXAMPLES_PATH.name} again and then rewrites the document."
                )
            self.stdout.write(f"{DOCUMENT_PATH.name} is current.")
            return
        DOCUMENT_PATH.write_text(current, encoding="utf-8")
        self.stdout.write(f"Wrote {DOCUMENT_PATH}.")
