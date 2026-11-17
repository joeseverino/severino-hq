"""A row's controls stand beside its words, and in a narrow box wait for their
row to open, shown there in place: the region marks the column, and no
control is ever drawn twice (docs/Design.md, A row's controls)."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import all_elements
from hq.platform.application.tables import TableColumn, ranked

_ROW = (
    '<tr><td><div class="row-line">Example title</div><p>Example words.</p></td>'
    '<td class="row-actions"><a class="btn compact" href="/example/open/" id="example-open">Open</a>'
    '<span class="btn compact" role="button" aria-disabled="true" tabindex="0">Refused</span>'
    '<div class="row-later"><a class="btn compact" href="/example/later/">Later</a></div></td></tr>'
)


def _table(
    rows: str,
    head: str = '{% table_heading "Name" gutter=True %}{% table_heading "Do" "control" hidden=True drop=1 %}',
    **context,
) -> str:
    source = (
        '{% load table_tags %}{% table_region "Examples" %}<table class="data-table"><thead><tr>'
        f"{head}</tr></thead><tbody>{rows}</tbody></table>{{% endtable_region %}}"
    )
    return Template(source).render(Context(context))


def _leaving(html: str) -> list:
    """The cells that leave the row in a narrow box."""

    return [each for each in all_elements(html) if each.tag in {"th", "td"} and "data-narrow" in each.attrs]


class RowControlsTests(SimpleTestCase):
    def test_a_rows_controls_leave_a_narrow_box_and_are_drawn_once(self):
        html = _table(_ROW)
        page = all_elements(html)
        self.assertEqual([each.tag for each in _leaving(html)], ["th", "td"])
        self.assertEqual([each.words for each in page if each.words == "Open" and each.tag == "a"], ["Open"])
        self.assertEqual(len([each for each in page if each.attrs.get("id") == "example-open"]), 1)

    def test_the_put_away_is_the_last_of_the_rows_buttons(self):
        """The reader's Dismiss is a button as the others are, read, reached and
        drawn after them."""

        from hq.platform.application.pages import PageAction

        row = (
            '<tr><td><div class="row-line">Example title</div></td>{% row_actions actions first=1 put_away=away %}</tr>'
        )
        actions = [PageAction("Open", "/example/open/"), PageAction("Later", "/example/later/")]
        away = PageAction._built(label="Dismiss", url="/example/aside/", method="post", field="key", value="example")
        for given in (actions, []):
            with self.subTest(actions=len(given)):
                html = _table(row, actions=given, away=away)
                (cell,) = [each for each in _leaving(html) if each.tag == "td"]
                shown = [each.words for each in all_elements(html) if cell in each.ancestors and "btn" in each.classes]
                self.assertEqual(shown, ["Open", "Dismiss"] if given else ["Dismiss"])

    def test_what_a_row_offers_only_when_open_moves_to_its_open_line(self):
        html = _table(_ROW)
        (cell,) = [each for each in _leaving(html) if each.tag == "td"]
        (later,) = [each for each in all_elements(html) if "row-later" in each.classes]
        self.assertNotIn(cell, later.ancestors)
        self.assertTrue(any("row-detail" in each.classes for each in later.ancestors))

    def test_a_column_of_choices_never_leaves(self):
        """A choice a reader makes is the row's value, so its column stays
        whatever rank its heading gives it, the words that stand where a row
        has no choice included."""

        rows = (
            '<tr><td>Example</td><td><select name="example"><option>One</option></select></td></tr>'
            '<tr><td>Example two</td><td><span class="muted">Not granted</span></td></tr>'
        )
        for head in ('{% table_heading "Rule" "control" %}', '{% table_heading "Rule" drop=1 %}'):
            with self.subTest(head=head):
                html = _table(rows, head='{% table_heading "Name" gutter=True %}' + head)
                self.assertEqual(_leaving(html), [])
                self.assertEqual(
                    [each.attrs.get("data-drop") for each in all_elements(html) if each.tag == "td"], [None] * 4
                )
                self.assertEqual(len([each for each in all_elements(html) if each.tag == "select"]), 1)

    def test_a_column_its_heading_sets_beneath_takes_no_rank(self):
        """Words every row must show (a change's two sides) never leave, and a
        cell that says nothing is marked so."""

        rows = "<tr><td>Example</td><td>one</td><td></td></tr>"
        head = '{% table_heading "Name" gutter=True %}{% table_heading "Before" beneath=True %}'
        html = _table(rows, head=head + '{% table_heading "After" beneath=True %}')
        cells = [each for each in all_elements(html) if each.tag == "td"][1:3]
        self.assertEqual(["data-blank" in each.attrs for each in cells], [False, True])
        self.assertEqual([each.attrs.get("data-drop") for each in cells], [None, None])

    def test_a_column_set_beneath_takes_no_rank(self):
        with self.assertRaisesMessage(ValueError, "stands beneath its row or leaves by its rank"):
            _table("<tr><td>Example</td><td>one</td></tr>", head='{% table_heading "Before" beneath=True drop=1 %}')

    def test_a_rows_state_never_leaves(self):
        """A pill says what the row is, so its column takes no rank."""

        row = '<tr><td>Example</td><td><span class="pill pill-good">Done</span></td></tr>'
        html = _table(row, head='{% table_heading "Name" gutter=True %}{% table_heading "Status" "status" drop=1 %}')
        cells = [each for each in all_elements(html) if each.tag in {"th", "td"}]
        self.assertEqual([each.attrs.get("data-drop") for each in cells], [None] * len(cells))
        self.assertEqual(_leaving(html), [])

    def test_a_pill_beside_a_value_is_a_note_whose_column_may_leave(self):
        row = '<tr><td>Example</td><td>192.0.2.10 <span class="pill pill-bad">Missing</span></td></tr>'
        html = _table(row, head='{% table_heading "Name" gutter=True %}{% table_heading "Address" drop=1 %}')
        cells = [each for each in all_elements(html) if each.tag == "td" and "colspan" not in each.attrs]
        self.assertIsNone(cells[0].attrs.get("data-drop"))
        self.assertIsNotNone(cells[1].attrs.get("data-drop"))
        self.assertEqual(_leaving(html), [])

    def test_a_list_never_ranks_its_column_of_controls_to_leave(self):
        columns = ranked((TableColumn("Name"), TableColumn("Note"), TableColumn("", css="actions-col")))
        self.assertEqual([column.drop for column in columns], [0, 1, 0])
