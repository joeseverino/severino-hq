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
    '<div class="narrow-more row-later"><a class="btn compact" href="/example/later/">Later</a></div></td>'
    '<td class="row-actions" data-put-away><button class="btn compact ghost" form="example">Dismiss</button></td></tr>'
)


def _table(
    rows: str, head: str = '{% table_heading "Name" gutter=True %}{% table_heading "Do" "control" hidden=True drop=1 %}'
) -> str:
    source = (
        '{% load table_tags %}{% table_region "Examples" %}<table class="data-table"><thead><tr>'
        f"{head}</tr></thead><tbody>{rows}</tbody></table>{{% endtable_region %}}"
    )
    return Template(source).render(Context({}))


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

    def test_the_put_away_control_keeps_the_rows_first_line(self):
        html = _table(_ROW)
        (away,) = [each for each in all_elements(html) if "data-put-away" in each.attrs]
        self.assertEqual(away.tag, "td")
        self.assertNotIn("data-beneath", away.attrs)

    def test_the_column_of_controls_never_leaves(self):
        """Its cells and heading are marked for the narrow box to stack, so no
        rank sends its controls behind the row's toggle; its part of the open
        line holds only what the row keeps there."""

        html = _table(_ROW)
        self.assertEqual([each.tag for each in _marked(html)], ["th", "td"])
        detail = [each for each in all_elements(html) if "data-row-detail" in each.attrs]
        self.assertEqual([each.words for each in detail], ["Do Later"])

    def test_a_column_of_choices_is_no_rows_controls(self):
        """A choice is a value the row holds, so it keeps its column."""

        row = '<tr><td>Example</td><td><select name="example"><option>One</option></select></td></tr>'
        self.assertEqual(_marked(_table(row)), [])

    def test_a_list_never_ranks_its_column_of_controls_to_leave(self):
        columns = ranked((TableColumn("Name"), TableColumn("Note"), TableColumn("", css="actions-col")))
        self.assertEqual([column.drop for column in columns], [0, 1, 0])
