from typing import override

from django.core.management.base import BaseCommand, CommandError

from hq.platform.application.table_fit import DROP_STEPS, MOST_COLUMNS
from hq.platform.core.stylesheets import STATIC

SHEET = STATIC / "css" / "app" / "table-steps.css"

_HEAD = """\
/* Written by `manage.py table_steps` from `DROP_STEPS` and `MOST_COLUMNS`
   (hq/platform/application/table_fit.py): `mise run regen` rewrites it and
   `mise run drift` holds it to them. A container query reads no variable, so
   each width a column may leave at has its own rule, and a table's region
   marks the table and its cells with the widths that are theirs
   (table_tags.mark_cells). Below each width, from the widest, so the
   narrowest that holds comes last and wins: a table whose box is narrow
   there says so (`--box`), its columns share its width as it says
   (`data-columns`), and each column that leaves there goes, showing its row's
   toggle and keeping its table's gutter where it held anything. With no
   script nothing leaves. */
"""


def _counts(prefix: str) -> list[str]:
    return [
        f'.data-table[data-columns~="{prefix}{count}"] {{ --columns: {count}; }}'
        for count in range(1, MOST_COLUMNS + 1)
    ]


def _narrow(width: int) -> str:
    return (
        f'@container table-region (width < {width}px) {{ .data-table[data-narrow-at="{width}"] {{ --box: narrow; }} }}'
    )


def _leaving(width: int) -> list[str]:
    held = f'td[data-drop-at="{width}"]:not([data-blank])'
    rules = [
        f'.data-table > * > tr > [data-drop-at="{width}"] {{ display: none; }}',
        f".data-table > tbody > tr:has(> {held}) > * > [data-row-toggle] {{ display: inline-grid; }}",
        f".data-table:has(> tbody > tr > {held}) > * > tr > .row-gutter"
        " { padding-left: var(--row-gutter); --gutter: kept; }",
        *_counts(f"{width}:"),
    ]
    return [f"  @container table-region (width < {width}px) {{", *(f"    {rule}" for rule in rules), "  }"]


def sheet() -> str:
    widest_first = sorted(DROP_STEPS, reverse=True)
    lines = [
        _HEAD.rstrip(),
        "@layer components {",
        *_counts(""),
        *map(_narrow, widest_first),
        "@media (scripting: enabled) {",
        *(line for width in widest_first for line in _leaving(width)),
        "}",
        "}",
    ]
    return "\n".join(lines) + "\n"


class Command(BaseCommand):
    help = "Write css/app/table-steps.css from the declared drop widths, or --check it for drift."

    @override
    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Exit 1 when the sheet is behind.")

    @override
    def handle(self, *args, **options):
        current = sheet()
        if options["check"]:
            if not SHEET.exists() or SHEET.read_text(encoding="utf-8") != current:
                raise CommandError(f"{SHEET.name} is behind DROP_STEPS; run `mise run regen`.")
            self.stdout.write(f"{SHEET.name} is current.")
            return
        SHEET.write_text(current, encoding="utf-8")
        self.stdout.write(f"Wrote {SHEET.name}.")
