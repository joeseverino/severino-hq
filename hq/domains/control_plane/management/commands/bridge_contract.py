import difflib
from argparse import ArgumentParser
from typing import Any, override

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Write the controller's contracts from the registry: the bridge contract, the "
        "connections document's schema and the renderer's connection shapes. "
        "--check exits 1 on drift."
    )

    @override
    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit 1 when a committed file differs from the derived one.",
        )

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        from hq.domains.control_plane.bridge_contract import emitted

        stale = []
        for path, current in emitted().items():
            name = str(path.relative_to(settings.BASE_DIR))
            committed = path.read_text(encoding="utf-8") if path.exists() else ""
            if committed == current:
                continue
            if not options["check"]:
                path.write_text(current, encoding="utf-8")
                self.stdout.write(f"Wrote {name}.")
                continue
            stale.append(name)
            self.stderr.write(
                "".join(
                    difflib.unified_diff(
                        committed.splitlines(keepends=True),
                        current.splitlines(keepends=True),
                        f"committed/{name}",
                        f"derived/{name}",
                    )
                ),
                ending="",
            )
        if stale:
            raise CommandError(
                f"{', '.join(stale)} is behind the registry; run `manage.py bridge_contract`, "
                "then `go generate ./...` in controller/, and commit the result."
            )
        self.stdout.write("The controller's contracts are current.")
