"""A row's controls stand beside its words in a whole box and beneath them in
a narrow one, drawn once in their own cell: the region marks the cell, and the
stylesheet stacks the row (docs/Design.md, A row's controls)."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.tables import TableColumn, ranked
from hq.platform.application.tests.markup import all_elements

_ROW = (
    '<tr><td><div class="row-line">Example title</div><p>Example words.</p></td>'
    '<td class="row-actions"><a class="btn compact" href="/example/open/" id="example-open">Open</a>'
    '<span class="btn compact" role="button" aria-disabled="true" tabindex="0">Refused</span>'
    '<div class="narrow-more row-later"><a class="btn compact" href="/example/later/">Later</a></div></td></tr>'
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


def _marked(html: str) -> list:
    return [each for each in all_elements(html) if "data-beneath" in each.attrs]


class RowControlsTests(SimpleTestCase):
    def test_a_rows_controls_are_marked_to_stand_beneath_its_words_and_drawn_once(self):
        html = _table(_ROW)
        page = all_elements(html)
        (cell,) = [each for each in _marked(html) if each.tag == "td"]
        self.assertIn("row-actions", cell.classes)
        # One of each control on the page, so each id and each form is drawn once.
        self.assertEqual([each.words for each in page if "btn" in each.classes].count("Open"), 1)
        self.assertEqual(len([each for each in page if each.attrs.get("id") == "example-open"]), 1)

    def test_the_put_away_is_the_last_of_the_rows_buttons(self):
        """The reader's Dismiss is a button as the others are, read, reached and
        drawn after them; a row whose only control it is stacks it the same."""

        from hq.platform.application.pages import PageAction

        row = (
            '<tr><td><div class="row-line">Example title</div></td>{% row_actions actions first=1 put_away=away %}</tr>'
        )
        actions = [PageAction("Open", "/example/open/"), PageAction("Later", "/example/later/")]
        away = PageAction("Dismiss", "/example/aside/", method="post", field="key", value="example")
        for given in (actions, []):
            with self.subTest(actions=len(given)):
                html = _table(row, actions=given, away=away)
                (cell,) = [each for each in _marked(html) if each.tag == "td"]
                shown = [
                    each.words
                    for each in all_elements(html)
                    if cell in each.ancestors
                    and "btn" in each.classes
                    and not each.beneath
                    and not any("row-later" in ancestor.classes for ancestor in each.ancestors)
                ]
                self.assertEqual(shown, ["Open", "Dismiss"] if given else ["Dismiss"])

    def test_the_column_of_controls_never_leaves(self):
        """Its cells and heading are marked for the narrow box to stack, so no
        rank sends its controls behind the row's toggle; what it offers only
        on an open row stays in its cell, shown there once the row opens."""

        html = _table(_ROW)
        marked = _marked(html)
        self.assertEqual([each.tag for each in marked], ["th", "td"])
        later = [each for each in all_elements(html) if "row-later" in each.classes]
        self.assertEqual([marked[1] in each.ancestors for each in later], [True])
        self.assertEqual([each for each in all_elements(html) if "row-columns" in each.classes], [])

    def test_a_column_of_choices_stands_beneath_as_controls_do(self):
        """A choice a reader makes is a control of the row, so in a narrow box
        it stacks beneath the row's words under its column's name."""

        row = '<tr><td>Example</td><td><select name="example"><option>One</option></select></td></tr>'
        self.assertEqual([each.tag for each in _marked(_table(row))], ["th", "td"])

    def test_the_words_a_column_of_choices_holds_stand_beneath_under_its_name(self):
        """A row with no choice to make says so in the column of choices, and
        in a narrow box those words stack beneath with the choices of the
        other rows, under the column's name: left in their column they would
        hold it open beside every row's words."""

        rows = (
            '<tr><td>Example</td><td><select name="example"><option>One</option></select></td></tr>'
            '<tr><td>Example two</td><td><span class="muted">Not granted</span></td></tr>'
        )
        html = _table(rows, head='{% table_heading "Name" gutter=True %}{% table_heading "Rule" "control" %}')
        cells = [each for each in _marked(html) if each.tag == "td"]
        self.assertEqual([each.attrs.get("data-label") for each in cells], ["Rule", "Rule"])

    def test_a_column_its_heading_sets_beneath_stands_under_its_name(self):
        """Words every row must show (a change's two sides) take no rank: in a
        narrow box each stands beneath the row's words under its column's
        name, and a cell that says nothing is marked so it takes no line."""

        rows = "<tr><td>Example</td><td>one</td><td></td></tr>"
        head = '{% table_heading "Name" gutter=True %}{% table_heading "Before" beneath=True %}'
        html = _table(rows, head=head + '{% table_heading "After" beneath=True %}')
        cells = [each for each in _marked(html) if each.tag == "td"]
        self.assertEqual([each.attrs.get("data-label") for each in cells], ["Before", "After"])
        self.assertEqual(["data-blank" in each.attrs for each in cells], [False, True])
        self.assertEqual([each.attrs.get("data-drop") for each in cells], [None, None])

    def test_a_column_set_beneath_takes_no_rank(self):
        with self.assertRaisesMessage(ValueError, "stands beneath its row or leaves by its rank"):
            _table("<tr><td>Example</td><td>one</td></tr>", head='{% table_heading "Before" beneath=True drop=1 %}')

    def test_a_column_of_choices_never_leaves_with_its_rank(self):
        """A choice a reader makes is what the row is for, so the narrow box
        keeps its column whatever rank its heading gives it."""

        row = '<tr><td>Example</td><td><select name="example"><option>One</option></select></td></tr>'
        html = _table(row, head='{% table_heading "Name" gutter=True %}{% table_heading "Rule" drop=1 %}')
        self.assertEqual([each.attrs.get("data-drop") for each in all_elements(html) if each.tag == "td"], [None, None])

    def test_a_rows_state_never_leaves_and_stands_beneath_its_words(self):
        """A pill says what the row is, so its column takes no rank and, in a
        narrow box, stands beneath the row's words as its controls do."""

        row = '<tr><td>Example</td><td><span class="pill pill-good">Done</span></td></tr>'
        html = _table(row, head='{% table_heading "Name" gutter=True %}{% table_heading "Status" "status" drop=1 %}')
        cells = [each for each in all_elements(html) if each.tag in {"th", "td"}]
        self.assertEqual([each.attrs.get("data-drop") for each in cells], [None, None, None, None])
        self.assertEqual([each.tag for each in _marked(html)], ["th", "td"])

    def test_a_pill_beside_a_value_is_a_note_whose_column_may_leave(self):
        row = '<tr><td>Example</td><td>192.0.2.10 <span class="pill pill-bad">Missing</span></td></tr>'
        html = _table(row, head='{% table_heading "Name" gutter=True %}{% table_heading "Address" drop=1 %}')
        self.assertEqual([each.attrs.get("data-drop") for each in all_elements(html) if each.tag == "td"], [None, "1"])
        self.assertEqual(_marked(html), [])

    def test_a_list_never_ranks_its_column_of_controls_to_leave(self):
        columns = ranked((TableColumn("Name"), TableColumn("Note"), TableColumn("", css="actions-col")))
        self.assertEqual([column.drop for column in columns], [0, 1, 0])
