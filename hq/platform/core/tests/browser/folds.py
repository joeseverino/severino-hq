"""Folds as a reader opens them: a row that heads rows folded under it, and
a fold within a fold, which the survey opens to judge what it holds."""

from hq.platform.application.tables import Cell, DataTable, Row, TableColumn
from hq.platform.core.tests.browser.gate import with_script
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.survey import CLOSE_OPENED, open_every_fold
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE

MESSAGE = "A sentence about the event, long enough that a phone has no room for it beside the rest"


def _event() -> DataTable:
    """An event that heads the row folded under it, its message too long for a phone."""

    columns = (
        TableColumn("What"),
        TableColumn("When", css="date-col key-col"),
        TableColumn("Who", css="optional-col", drop=1),
        TableColumn("Message", drop=2),
    )
    rows = (
        Row(
            (
                Cell("Example event", name=True, fold=True),
                Cell("Nov 1"),
                Cell("example-actor"),
                Cell(MESSAGE, role="data"),
            ),
            folds="example-event",
        ),
        Row(
            (Cell("Example item"), Cell("Nov 1"), Cell("example-actor"), Cell("Changed", role="data")),
            folded="example-event",
        ),
    )
    return DataTable("Events", columns, rows)


# What the event's columns that left say on its open line, as a reader sees it.
SAID = """(id) => {
  const line = document.getElementById(id).nextElementSibling;
  if (!line?.matches('[data-row-detail]')) return [];
  return [...line.querySelectorAll('.row-columns > [data-drop] > dd')]
    .filter((part) => part.checkVisibility()).map((part) => part.textContent.trim());
}"""
PRESS_FOLD = "(id) => document.getElementById(id).querySelector('[data-fold-toggle]').click()"
# A value cut in a row with no toggle to open it, in a table inside a folded
# section inside another, as a hop's evidence lies inside its hop.
NESTED = (
    '{% section "Example outer" fold=True %}<p>Example</p>{% section "Example inner" fold=True %}'
    '<table class="data-table"><tbody><tr><td>'
    '<span style="display: block; inline-size: 10rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">'
    "an-example-value-far-too-long-for-its-cell</span></td></tr></tbody></table>"
    "{% endsection %}{% endsection %}"
)
CLOSED = "() => document.querySelectorAll('details:not([open])').length"
# The rows in the order a press on the first heading sorts them.
SORTED = """() => {
  document.querySelector('[data-sort-local]').click();
  return [...document.querySelectorAll('main .data-table > tbody > tr:not(.row-detail)')]
    .map((row) => row.cells[0].textContent.trim());
}"""


def _sorted_fold() -> DataTable:
    """A row heading a fold, a row and a row across the table under it, and a
    row its sort puts first."""

    columns = (TableColumn("What"), TableColumn("When"))
    return DataTable(
        "Events",
        columns,
        (
            Row((Cell("Example second", fold=True), Cell("Nov 2")), folds="example-second"),
            Row((Cell("Example item"), Cell("Nov 2")), folded="example-second"),
            Row((Cell("Example rest"),), folded="example-second", across=True),
            Row((Cell("Example first"), Cell("Nov 1"))),
        ),
    )


class FoldBrowserTests(LayoutBrowserTests):
    """The fold stands where the row's own toggle would, so it opens both."""

    @with_script
    def test_a_row_that_heads_a_fold_opens_what_its_columns_that_left_hold(self):
        """On a phone, opening the fold shows who and what the event's dropped
        columns say beside its rows, and closing it hides them again."""

        self.pages = {**self.pages, "event-probe": probe("{% data_table table %}", table=_event())}
        self.open("event-probe", PHONE_SCREEN[0])
        self.assertEqual(self.page.evaluate(SAID, "example-event"), [])

        self.page.evaluate(PRESS_FOLD, "example-event")
        said = self.page.evaluate(SAID, "example-event")
        self.assertIn("example-actor", said)
        self.assertIn(MESSAGE, said)

        self.page.evaluate(PRESS_FOLD, "example-event")
        self.assertEqual(self.page.evaluate(SAID, "example-event"), [])

    @with_script
    def test_a_sort_on_the_page_keeps_a_folds_rows_under_the_row_that_heads_them(self):
        self.pages = {**self.pages, "sorted-probe": probe("{% data_table table %}", table=_sorted_fold())}
        self.open("sorted-probe", WIDE)

        self.assertEqual(
            self.page.evaluate(SORTED), ["Example first", "Example second", "Example item", "Example rest"]
        )

    @with_script
    def test_a_defect_in_a_fold_within_a_fold_is_found_once_every_fold_is_open_and_the_folds_close_again(self):
        self.pages = {**self.pages, "nested-probe": probe(NESTED)}
        self.open("nested-probe", WIDE)
        self.assertEqual(self.page.evaluate(CLOSED), 2)
        self.assertNotIn("cut and closed", self.findings("nested-probe", WIDE))

        opened, short = open_every_fold(self)
        self.assertEqual((opened, short), (2, ""))
        self.assertEqual(self.page.evaluate(CLOSED), 0)
        self.assertIn("cut and closed", self.findings("nested-probe", WIDE))

        self.page.evaluate(CLOSE_OPENED)
        self.assertEqual(self.page.evaluate(CLOSED), 2)
