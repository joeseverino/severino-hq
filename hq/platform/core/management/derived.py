import difflib
from pathlib import Path
from typing import override

from django.core.management.base import BaseCommand, CommandError


class DerivedFileCommand(BaseCommand):
    """A command that writes one file HQ derives from its declarations, or with
    --check fails while the committed copy is behind them, writing nothing and
    printing how the two differ."""

    source = "its declarations"

    def target(self) -> Path:
        raise NotImplementedError

    def derive(self) -> str:
        raise NotImplementedError

    @override
    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Exit 1 when the committed file is behind.")

    @override
    def handle(self, *args, **options):
        current = self.derive()
        path = self.target()
        if options["check"]:
            committed = path.read_text(encoding="utf-8") if path.exists() else ""
            if committed != current:
                self.stderr.write(
                    "".join(
                        difflib.unified_diff(
                            committed.splitlines(keepends=True),
                            current.splitlines(keepends=True),
                            f"committed/{path.name}",
                            f"derived/{path.name}",
                        )
                    ),
                    ending="",
                )
                command = self.__module__.rpartition(".")[2]
                raise CommandError(f"{path.name} is behind {self.source}; run `mise run manage -- {command}`.")
            self.stdout.write(f"{path.name} is current.")
            return
        path.write_text(current, encoding="utf-8")
        self.stdout.write(f"Wrote {path.name}.")
