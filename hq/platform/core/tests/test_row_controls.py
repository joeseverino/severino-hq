"""A row's controls stand in the row at every width, beside its words or on its
last line, and no control is ever drawn twice (``row_actions``'s rules, A row's
controls)."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import all_elements
from hq.platform.application.tables import TableColumn, ranked

_ROW = (
    '<tr><td><div class="row-line">Example title</div><p>Example words.</p></td>'
    '<td class="row-actions"><a class="btn compact" href="/example/open/" id="example-open">Open</a>'
    '<span class="btn compact" role="button" aria-disabled="true" tabindex="0">Refused</span></td></tr>'
)


def _table(
    rows: str,
    head: str = '{% table_heading "Name" %}{% table_heading "Do" "control" hidden=True drop=1 %}',
    **context,
) -> str:
    source = (
        '{% load table_tags %}{% table_region "Examples" %}<table class="data-table"><thead><tr>'
        f"{head}</tr></thead><tbody>{rows}</tbody></table>{{% endtable_region %}}"
    )
    return Template(source).render(Context(context))


def _controls(html: str) -> list:
    """The body cells of the rows' controls."""

    return [each for each in all_elements(html) if each.tag == "td" and each.attrs.get("data-kind") == "control"]


def _ranks(html: str) -> list:
    """The rank each heading leaves at, None for a column that stays."""

    return [each.attrs.get("data-drop") for each in all_elements(html) if each.tag == "th"]


class RowControlsTests(SimpleTestCase):
    def test_a_rows_controls_stand_at_every_width_and_are_drawn_once(self):
        html = _table(_ROW)
        page = all_elements(html)
        self.assertEqual(_ranks(html), [None, None])
        self.assertEqual(len(_controls(html)), 1)
        self.assertEqual([each.words for each in page if each.words == "Open" and each.tag == "a"], ["Open"])
        self.assertEqual(len([each for each in page if each.attrs.get("id") == "example-open"]), 1)

    def test_the_put_away_is_the_last_of_the_rows_buttons(self):
        """The reader's Dismiss is a button as the others are, read, reached and
        drawn after them."""

        from hq.platform.application.pages import PageAction

        row = '<tr><td><div class="row-line">Example title</div></td>{% row_actions actions put_away=away %}</tr>'
        actions = [PageAction("Open", "/example/open/"), PageAction("Later", "/example/later/")]
        away = PageAction._built(label="Dismiss", url="/example/aside/", method="post", field="key", value="example")
        for given in (actions, []):
            with self.subTest(actions=len(given)):
                html = _table(row, actions=given, away=away)
                (cell,) = _controls(html)
                shown = [each.words for each in all_elements(html) if cell in each.ancestors and "btn" in each.classes]
                self.assertEqual(shown, ["Open", "Later", "Dismiss"] if given else ["Dismiss"])

    def test_every_action_a_row_offers_stands_in_its_cell(self):
        """No control waits on a row's open line: they stand side by side in
        the row and wrap there only when it runs out of room."""

        from hq.platform.application.pages import PageAction

        row = '<tr><td><div class="row-line">Example title</div></td>{% row_actions actions %}</tr>'
        actions = [PageAction("Open", "/example/open/"), PageAction("Check", "/example/check/")]
        html = _table(row, actions=actions)
        (cell,) = _controls(html)
        buttons = [each for each in all_elements(html) if "btn" in each.classes]
        self.assertEqual([each.words for each in buttons if cell in each.ancestors], ["Open", "Check"])
        self.assertFalse([each for each in buttons if each.beneath])

    def test_a_row_with_no_heading_over_it_opens_to_what_it_moves(self):
        """What a row moves to its open line is reached by the row's toggle,
        which the region draws in its first cell in a table with no headings."""

        source = (
            '{% load table_tags %}{% table_region "Examples" %}<table class="data-table"><tbody>'
            '<tr><td><strong>Example</strong><div class="row-below">More about it.</div></td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        page = all_elements(Template(source).render(Context()))
        (row,) = [each for each in page if each.tag == "tr" and "data-below" in each.attrs]
        self.assertTrue([each for each in page if row in each.ancestors and "aria-expanded" in each.attrs])

    def test_a_list_never_ranks_its_column_of_controls_to_leave(self):
        columns = ranked((TableColumn("Name"), TableColumn("Note"), TableColumn("", css="actions-col")))
        self.assertEqual([column.drop for column in columns], [0, 1, 0])
