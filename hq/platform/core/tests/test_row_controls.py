"""A row's controls stand in the row at every width, beside its words or on its
last line, and no control is ever drawn twice (``row_actions``'s rules, A row's
controls)."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import all_elements
from hq.platform.application.pages import PageAction
from hq.platform.application.tables import Below, Cell, DataTable, Row, TableColumn
from hq.platform.core.testing import leaving

_ACTIONS = (PageAction("Open", "/example/open/"), PageAction("Check", "/example/check/"))


def _table(*rows: Row, headed: bool = True) -> str:
    table = DataTable("Examples", (TableColumn("Name"),), rows, headed=headed)
    return Template("{% data_table table %}").render(Context({"table": table}))


def _controls(html: str) -> list:
    """The body cells of the rows' controls."""

    return [each for each in all_elements(html) if each.tag == "td" and each.attrs.get("data-kind") == "control"]


class RowControlsTests(SimpleTestCase):
    def test_a_rows_controls_stand_at_every_width_and_are_drawn_once(self):
        refused = PageAction("Refused", "/example/refused/", disabled=True, title="Example reason.")
        html = _table(Row((Cell("Example title", lines=(Cell("Example words."),)),), actions=(_ACTIONS[0], refused)))
        page = all_elements(html)
        self.assertEqual([each.attrs.get("data-drop") for each in page if each.tag == "th"], [None, None])
        self.assertEqual(len(_controls(html)), 1)
        self.assertEqual([each.words for each in page if each.words == "Open" and each.tag == "a"], ["Open"])

    def test_the_put_away_is_the_last_of_the_rows_buttons(self):
        """The reader's Dismiss is a button as the others are, read, reached and
        drawn after them."""

        away = PageAction._built(label="Dismiss", url="/example/aside/", method="post", field="key", value="example")
        for given in (_ACTIONS, ()):
            with self.subTest(actions=len(given)):
                html = _table(Row((Cell("Example title"),), actions=given, put_away=away))
                (cell,) = _controls(html)
                shown = [each.words for each in all_elements(html) if cell in each.ancestors and "btn" in each.classes]
                self.assertEqual(shown, ["Open", "Check", "Dismiss"] if given else ["Dismiss"])

    def test_every_action_a_row_offers_stands_in_its_cell(self):
        """No control waits on a row's open line: they stand side by side in
        the row and wrap there only when it runs out of room."""

        html = _table(Row((Cell("Example title"),), actions=_ACTIONS))
        (cell,) = _controls(html)
        buttons = [each for each in all_elements(html) if "btn" in each.classes]
        self.assertEqual([each.words for each in buttons if cell in each.ancestors], ["Open", "Check"])
        self.assertFalse([each for each in buttons if each.beneath])

    def test_a_row_with_no_heading_over_it_opens_to_what_it_moves(self):
        """What a row moves to its open line is reached by the row's toggle,
        which the table draws in its first cell when it has no headings."""

        moved = Cell("Example", name=True, below=(Below("", (Cell("More about it."),)),))
        page = all_elements(_table(Row((moved,)), headed=False))
        (row,) = [each for each in page if each.tag == "tr" and "data-below" in each.attrs]
        self.assertTrue([each for each in page if row in each.ancestors and "aria-expanded" in each.attrs])

    def test_a_list_never_ranks_its_column_of_controls_to_leave(self):
        levels = leaving(TableColumn("Name"), TableColumn("Note"), TableColumn("", css="actions-col"))
        self.assertEqual(list(levels.values()), [0, 4, 0])
