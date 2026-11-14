"""A row's controls stand beside its words in a whole box and beneath them in
a narrow one, from one declaration: the region copies them into the cell that
names the row (docs/Design.md, A row's controls)."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.tables import TableColumn, ranked
from hq.platform.application.tests.markup import all_elements, with_class

_ROW = (
    '<tr><td><div class="row-line" data-row-line><span>Example title</span></div><p>Example words.</p></td>'
    '<td class="row-actions"><a class="btn compact" href="/example/open/">Open</a>'
    '<span class="btn compact" role="button" aria-disabled="true" tabindex="0">Refused</span>'
    '<div class="narrow-more row-later"><a class="btn compact" href="/example/later/">Later</a></div>'
    '<span data-put-away><button class="btn compact ghost" form="example">Dismiss</button></span></td></tr>'
)


def _table(
    rows: str, head: str = '{% table_heading "Name" gutter=True %}{% table_heading "Do" "control" hidden=True drop=1 %}'
) -> str:
    source = (
        '{% load table_tags %}{% table_region "Examples" %}<table class="data-table"><thead><tr>'
        f"{head}</tr></thead><tbody>{rows}</tbody></table>{{% endtable_region %}}"
    )
    return Template(source).render(Context({}))


def _said(element) -> list[str]:
    return [each.words for each in element if each.tag in ("a", "button", "span") and "btn" in each.classes]


class RowControlsTests(SimpleTestCase):
    def test_a_rows_controls_are_copied_beneath_its_words_for_a_narrow_box(self):
        page = all_elements(_table(_ROW))
        (beneath,) = [each for each in page if "row-beneath" in each.classes]
        self.assertIn("narrow-only", beneath.classes)
        self.assertEqual(beneath.ancestors[-1].tag, "td")
        self.assertNotIn("row-actions", beneath.ancestors[-1].classes)
        copied = [each for each in page if beneath in each.ancestors]
        # What it offers, a refused one included; what waits for the open line stays there.
        self.assertEqual(_said(copied), ["Open", "Refused"])

    def test_the_put_away_control_ends_the_rows_first_line(self):
        page = all_elements(_table(_ROW))
        (away,) = [each for each in page if "row-put-away" in each.classes]
        self.assertIn("narrow-only", away.classes)
        self.assertIn("data-row-line", away.ancestors[-1].attrs)
        self.assertEqual(_said([each for each in page if away in each.ancestors]), ["Dismiss"])

    def test_a_rows_put_away_control_goes_beneath_where_no_line_is_marked(self):
        row = _ROW.replace(' class="row-line" data-row-line', "")
        page = all_elements(_table(row))
        self.assertEqual(with_class(_table(row), "row-put-away"), [])
        (beneath,) = [each for each in page if "row-beneath" in each.classes]
        self.assertIn("Dismiss", _said([each for each in page if beneath in each.ancestors]))

    def test_the_column_of_controls_empties_and_never_leaves(self):
        """Its cells and heading are marked for the narrow box to empty, so no
        rank sends its controls behind the row's toggle; its part of the open
        line holds only what the row keeps there."""

        html = _table(_ROW)
        marked = [each.tag for each in all_elements(html) if "data-beneath" in each.attrs]
        self.assertEqual(marked, ["th", "td"])
        detail = [each for each in all_elements(html) if "data-row-detail" in each.attrs]
        self.assertEqual([each.words for each in detail], ["Do Later"])

    def test_a_column_of_choices_is_no_rows_controls(self):
        """Two of a field would both be sent, and a choice is a value the row
        holds, so it keeps its column."""

        row = '<tr><td>Example</td><td><select name="example"><option>One</option></select></td></tr>'
        html = _table(row)
        self.assertEqual(with_class(html, "row-beneath"), [])
        self.assertFalse([each for each in all_elements(html) if "data-beneath" in each.attrs])

    def test_a_list_never_ranks_its_column_of_controls_to_leave(self):
        columns = ranked((TableColumn("Name"), TableColumn("Note"), TableColumn("", css="actions-col")))
        self.assertEqual([column.drop for column in columns], [0, 1, 0])
