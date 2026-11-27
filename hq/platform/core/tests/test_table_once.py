"""The bytes a declared table's row and a page with no rows draw hold to
their budgets (``data_table``'s rules, Opening a row moves no column)."""

import re

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from hq.platform.application.markup_testing import with_class
from hq.platform.application.pages import PageAction
from hq.platform.application.tables import Below, Cell, DataTable, Row, TableColumn
from hq.platform.core.testing import SignedInTestCase

# The bytes a row of `test_a_rows_bytes_hold_to_their_budget`'s table
# may draw, its cells and their marks written once.
ROW_BYTES = 1_700
# The bytes a signed-in page of a list with no rows may draw: the page's
# chrome, every frame it carries for its scripts, and the list's empty state.
CHROME_BYTES = 16_700
# Assets by their plain names: an image's collected copies are named by their
# content, which lengthens every reference by its hash wherever it is served
# from one, so the markup is weighed alike in a checkout and in an image.
_PLAIN_ASSETS = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _drawn(table: DataTable) -> str:
    return Template("{% data_table table %}").render(Context({"table": table}))


class RowBytesTests(SimpleTestCase):
    def test_a_rows_bytes_hold_to_their_budget(self):
        """Bytes per row of a table like the connections list, whose rows hold
        a ranked column, a list's rest and a block beneath: a number load does
        not move, so it holds the cut of writing each cell once."""

        reads = ("DNS client", "DNS lookups", "DNS server", "Internal DNS record")

        def row(n: int) -> Row:
            return Row(
                (
                    Cell(
                        f"example-{n}",
                        url=f"/example/{n}/",
                        name=True,
                        lines=(
                            Cell(f"192.0.2.{n}:8080", role="data"),
                            Cell("Reached at example-host", muted=True, shows="narrow-more"),
                        ),
                        below=(Below("Changes", (Cell("A record"),)),),
                    ),
                    Cell(
                        fit=True,
                        parts=tuple(Cell(read) for read in reads),
                    ),
                    Cell("example-host", url="/example/host/"),
                    Cell("Working", tone="good", lines=(Cell("Full access", muted=True, shows="more"),)),
                ),
                id=f"row-{n}",
                actions=(PageAction("Edit", f"/example/{n}/edit/"),),
            )

        rows = 20
        columns = (
            TableColumn("Connection", kind="identifier"),
            TableColumn("Reads", drop=2),
            TableColumn("Reaches", drop=1),
            TableColumn("State", kind="status"),
        )
        html = _drawn(DataTable("Things", columns, tuple(row(n) for n in range(rows))))
        body = re.search(r"<tbody>.*</tbody>", html, re.DOTALL).group(0)

        self.assertLessEqual(len(body.encode()) / rows, ROW_BYTES)


@override_settings(STORAGES=_PLAIN_ASSETS)
class EmptyListBytesTests(SignedInTestCase):
    def test_a_page_with_no_rows_holds_to_its_chrome_budget(self):
        """What every page carries, read where no row adds to it: a number load
        does not move, so a frame or a part added to every page is weighed."""

        page = self.client.get(reverse("expenses:list"))
        # The sections' list grows with what is composed, so it is left out.
        chrome = re.sub(r'<nav class="primary-nav".*?</nav>', "", page.content.decode(), flags=re.DOTALL)

        self.assertEqual(with_class(page, "data-table"), [])
        self.assertLessEqual(len(chrome.encode()), CHROME_BYTES)
