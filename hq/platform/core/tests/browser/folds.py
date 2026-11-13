"""Folds as a reader opens them: a row that heads rows folded under it, and
a fold within a fold, which the survey opens to judge what it holds."""

from hq.platform.core.tests.browser.gate import with_script
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.survey import CLOSE_OPENED, open_every_fold
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE

MESSAGE = "A sentence about the event, long enough that a phone has no room for it beside the rest"
EVENT = (
    '{% table_region "Events" room="shared" %}<table class="data-table"><thead><tr>'
    '{% table_heading "What" gutter=True %}{% table_heading "When" css="date-col key-col" %}'
    '{% table_heading "Who" css="optional-col" drop=1 %}{% table_heading "Message" drop=2 %}</tr></thead><tbody>'
    '<tr id="example-event" data-folds><td class="row-gutter">'
    '{% include "partials/_row_fold.html" with label="Example event" only %}<strong>Example event</strong>'
    '</td><td>Nov 1</td><td>example-actor</td><td class="cell-data"><div style="min-width: 320px">{{ message }}</div></td></tr>'
    '<tr data-folded="example-event"><td>Example item</td>'
    '<td>Nov 1</td><td>example-actor</td><td class="cell-data">Changed</td></tr>'
    "</tbody></table>{% endtable_region %}"
)
# What the line beneath the event says, as a reader sees it.
SAID = """(id) => [...document.getElementById(id).nextElementSibling.querySelectorAll('dd')]
  .filter((part) => part.checkVisibility()).map((part) => part.textContent.trim())"""
PRESS_FOLD = "(id) => document.getElementById(id).querySelector('[data-fold-toggle]').click()"
# A value cut in a row with no toggle to open it, in a table inside a folded
# section inside another, as a hop's evidence lies inside its hop.
NESTED = (
    '{% section "Example outer" fold=True %}<p>Example</p>{% section "Example inner" fold=True %}'
    '<table class="data-table" style="table-layout: fixed; inline-size: 10rem"><tbody><tr><td>'
    '<span style="display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">'
    "an-example-value-far-too-long-for-its-cell</span></td></tr></tbody></table>"
    "{% endsection %}{% endsection %}"
)
CLOSED = "() => document.querySelectorAll('details:not([open])').length"


class FoldBrowserTests(LayoutBrowserTests):
    """The fold stands where the row's own toggle would, so it opens both."""

    @with_script
    def test_a_row_that_heads_a_fold_opens_what_its_columns_that_left_hold(self):
        """On a phone, opening the fold shows who and what the event's dropped
        columns say beside its rows, and closing it hides them again."""

        self.pages = {**self.pages, "event-probe": probe(EVENT, message=MESSAGE)}
        self.open("event-probe", PHONE_SCREEN[0])
        self.assertEqual(self.page.evaluate(SAID, "example-event"), [])

        self.page.evaluate(PRESS_FOLD, "example-event")
        said = self.page.evaluate(SAID, "example-event")
        self.assertIn("example-actor", said)
        self.assertIn(MESSAGE, said)

        self.page.evaluate(PRESS_FOLD, "example-event")
        self.assertEqual(self.page.evaluate(SAID, "example-event"), [])

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
