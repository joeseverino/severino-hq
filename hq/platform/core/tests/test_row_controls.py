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
        rank sends its controls behind the row's toggle; its part of the open
        line holds only what the row keeps there."""

        html = _table(_ROW)
        self.assertEqual([each.tag for each in _marked(html)], ["th", "td"])
        line = [each for each in all_elements(html) if "row-columns" in each.classes]
        self.assertEqual([each.words for each in line], ["Do Later"])

    def test_the_heading_of_each_column_of_words_is_marked_as_its_words(self):
        """A narrow box gives a stacked row's room to its words, and their
        heading takes it on the heading's line too; a figure's never does, nor
        any heading of a table whose rows offer no controls."""

        def words(head, row):
            html = _table(row, head)
            return [each.words for each in all_elements(html) if each.tag == "th" and "data-words" in each.attrs]

        head = '{% table_heading "#" "numeric" gutter=True %}{% table_heading "Name" %}'
        row = "<tr><td>1</td><td>Example words</td>{}</tr>"
        control = '<td class="row-actions"><a class="btn compact" href="/example/">Open</a></td>'
        self.assertEqual(words(head + '{% table_heading "Do" "control" hidden=True %}', row.format(control)), ["Name"])
        self.assertEqual(words(head, row.format("")), [])

    def test_a_column_of_choices_is_no_rows_controls(self):
        """A choice is a value the row holds, so it keeps its column."""

        row = '<tr><td>Example</td><td><select name="example"><option>One</option></select></td></tr>'
        self.assertEqual(_marked(_table(row)), [])

    def test_a_list_never_ranks_its_column_of_controls_to_leave(self):
        columns = ranked((TableColumn("Name"), TableColumn("Note"), TableColumn("", css="actions-col")))
        self.assertEqual([column.drop for column in columns], [0, 1, 0])
