import difflib
from pathlib import Path
from typing import override

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.text import get_text_list

lazy from hq.platform.application.ui import agreed


class DerivedFileCommand(BaseCommand):
    """A command that writes the files HQ derives from its declarations, each
    whole, or with --check fails while a committed copy is behind them,
    writing nothing and printing how the two differ."""

    source = "its declarations"
    # What else a rewrite needs, said after the command that does it.
    then = ""

    def emitted(self) -> dict[Path, str]:
        """Each file this command writes, with its content."""

        raise NotImplementedError

    @override
    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Exit 1 when a committed file is behind.")

    @override
    def handle(self, *args, **options):
        stale = []
        for path, current in self.emitted().items():
            name = path.relative_to(settings.BASE_DIR) if path.is_relative_to(settings.BASE_DIR) else path.name
            committed = path.read_text(encoding="utf-8") if path.exists() else ""
            if committed == current:
                self.stdout.write(f"{name} is current.")
            elif not options["check"]:
                path.write_text(current, encoding="utf-8")
                self.stdout.write(f"Wrote {name}.")
            else:
                stale.append(str(name))
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
            command = self.__module__.rpartition(".")[2]
            raise CommandError(
                f"{get_text_list(stale, 'and')} {agreed(len(stale), 'is', 'are')} behind {self.source}; "
                f"run `mise run manage -- {command}`{self.then}."
            )
