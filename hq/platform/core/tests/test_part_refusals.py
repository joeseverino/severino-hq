"""What each shared part refuses beyond what EveryPartTests writes for every
part (an unknown option, a required one left out, a value outside its
choices): a pairing of options a part cannot draw, refused where it is
written or drawn."""

from django import forms
from django.template import Context, RequestContext, Template, TemplateSyntaxError, engines
from django.test import RequestFactory, SimpleTestCase

from hq.platform.application.page_parts import SectionPart
from hq.platform.application.pages import PageAction, RowOrder
from hq.platform.application.printable import Unprintable
from hq.platform.application.tables import Cell, DataTable, Row, RowEditor, TableColumn
from hq.platform.core.table_layout import TRACKS
from hq.platform.core.templatetags.action_tags import MOST_BESIDE, head_actions
from hq.platform.core.templatetags.status_tags import status_pill
from hq.platform.core.templatetags.value_tags import cell_line, or_empty
from hq.platform.core.testing import leaving, render


def drawn(source: str, request=None, **context) -> str:
    template = Template(source)
    return template.render(RequestContext(request, context) if request else Context(context))


def moves_up():
    """A row's press that moves it up, an offer built where its command was read."""

    (up,) = RowOrder("/example/7/", "example 7", up=True, down=False).presses
    return up


class ListRefusalTests(SimpleTestCase):
    def test_an_option_of_another_kind_is_refused(self):
        for written in ('kind="dots" below="Affected"', "muted=True", 'kind="chips" numbered=True'):
            with self.subTest(written=written), self.assertRaisesMessage(TemplateSyntaxError, "is for kind="):
                render(f"{{% list {written} %}}<li>example.com</li>{{% endlist %}}")

    def test_words_outside_an_item_are_refused(self):
        """Words between the items would join a run with no dot before them,
        or stand outside every line."""

        for items in ("Online<li>example-host</li>", "<li>Online</li> since today"):
            with self.subTest(items=items), self.assertRaises(TemplateSyntaxError):
                render(f'{{% list kind="dots" %}}{items}{{% endlist %}}')

    def test_a_line_list_with_no_name_to_draw_each_value_by_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{% line_list names %}{% endline_list %}")

    def test_present_takes_one_value(self):
        with self.assertRaises(TemplateSyntaxError):
            render("{% present a b %}x{% endpresent %}")


class SelectRefusalTests(SimpleTestCase):
    def refused(self, source):
        with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
            drawn(source, options=(("", "Default"), ("allow", "On")))

    def test_a_choice_that_names_its_own_default_is_a_pick_and_never_any(self):
        self.refused('{% select "view" options every="view" %}')

    def test_a_tone_outside_the_status_vocabulary_and_a_select_with_no_name_are_refused(self):
        self.refused('{% select "rule" options tone="allow" %}')
        self.refused('{% select "" options %}')


class FilterBarRefusalTests(SimpleTestCase):
    def test_a_count_with_no_total_a_hold_with_no_way_to_it_and_a_narrowing_with_no_way_back_are_refused(self):
        for source in (
            '{% filter_bar shown=3 %}<input name="q">{% endfilter_bar %}',
            '{% filter_bar shown=3 total=12 held=held %}<input name="q">{% endfilter_bar %}',
            '{% filter_bar filtered=True %}<input name="q">{% endfilter_bar %}',
        ):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                render(source, held=("3 readings",))


class ButtonRefusalTests(SimpleTestCase):
    """The one tag that draws a button refuses what it would drop or draw
    wrong, and posts only what an offer sends, through the one shared form,
    so no page draws a press the rule behind it was not asked about."""

    def test_what_a_button_would_drop_or_draw_wrong_is_refused(self):
        for written in (
            '{% button "Example" "/example/" look="invented" %}',
            '{% button "Example" "/example/" look="ghost" css="example-pin" %}',
            '{% button "Example" offer=press data_example=True %}',
            '{% button "Open" outside=True %}',
            '{% button "Open" "https://example.com/" outside=True data_example=1 %}',
            '{% button "Example" "/example/" post=True %}',
            '{% button "Example" offer=link %}',
            '{% button "Example" "/example/" offer=press %}',
            '{% button "Example" offer=press value="down" %}',
        ):
            with self.subTest(written), self.assertRaises(TemplateSyntaxError):
                render(written, press=moves_up(), link=PageAction("Example", "/example/"))

    def test_a_page_head_beside_its_title_holds_two_to_the_most(self):
        actions = [PageAction(label, f"/{label}/") for label in "abcde"]
        for wrong in (1, MOST_BESIDE + 1):
            with self.subTest(beside=wrong), self.assertRaises(ValueError):
                head_actions(actions, wrong)


class StatusRefusalTests(SimpleTestCase):
    def test_a_pairing_of_options_a_state_cannot_draw_is_refused(self):
        for options in (
            # Context ink is only for a dotted state.
            {"tone": "good", "ink": "context"},
            {"tone": "good", "ink": "context", "pill": False},
            {"tone": "good", "ink": "other", "dot": True},
            # A mark stands only before a state's words.
            {"tone": "good", "icon": "check-circle"},
            {"tone": "good", "icon": "check-circle", "pill": False, "dot": True},
            {"tone": "good", "icon": "check-circle", "chip": True},
            # A chip is a name, in no tone.
            {"chip": True, "tone": "good"},
            {"chip": True, "pill": False},
            {"chip": True, "dot": True},
        ):
            with self.subTest(options), self.assertRaises(ValueError):
                status_pill("Up", **options)

    def test_a_word_that_is_no_tone_is_refused_wherever_a_tone_colors_words(self):
        """A misspelled tone fails where it is drawn, never drawn as neutral,
        and a pill is never colored by a word of its own (a model's "archived",
        a resource's "drifted"): only a Tone or a subject's Toned state."""

        for source in (
            "{{ tone|tone_class }}",
            '{% status "Example" tone=tone %}',
            '{% status "Example" tone=tone dot=True %}',
            '{% status "Example" tone=tone pill=False %}',
            '{% status "Example" tone=tone icon="shield" pill=False %}',
        ):
            for word in ("goood", "archived", "drifted"):
                with self.subTest(source, word=word), self.assertRaises(ValueError):
                    engines["django"].from_string(source).render({"tone": word})


class SectionRefusalTests(SimpleTestCase):
    def test_a_section_written_needs_its_heading_and_a_link_to_the_rest_its_label(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{% section count=1 %}{% endsection %}")
        with self.assertRaises(ValueError):
            render('{% section "Renewals" more_url="/example/" %}<p>Body</p>{% endsection %}')

    def test_a_section_refuses_what_it_cannot_draw(self):
        for fields in (
            {"heading": ""},
            {"heading": "   "},
            {"heading": "<b></b>"},
            {"heading": "Receipts", "empty": "No receipts.", "remedy_label": "Upload one"},
            {"heading": "Receipts", "empty": "No receipts.", "remedy_url": "/receipts/new/"},
            # A folded section's toggle holds no link, and a figure does not fold.
            {"heading": "Runs", "fold": True, "more_label": "All runs", "more_url": "/example/"},
            {"heading": "Pace", "figure": True, "fold": True},
            {"heading": "Today", "when": "day"},
            {"heading": "Hosts", "level": 1},
            {"heading": "Hosts", "level": 7},
        ):
            with self.subTest(**fields), self.assertRaises(ValueError):
                SectionPart(**fields)


class TableRefusalTests(SimpleTestCase):
    def test_a_table_its_template_does_not_hold_is_refused_as_the_template_writes_it(self):
        with self.assertRaises(TypeError) as refused:
            drawn("{% data_table missing %}")
        self.assertIn("{% data_table missing %}", " ".join(refused.exception.__notes__))

    def test_a_column_refuses_what_cannot_be(self):
        for options in ({"kind": "color"}, {"css": "key-col", "drop": 1}, {"drop": -1}):
            with self.subTest(options), self.assertRaises(ValueError):
                TableColumn("Name", **options)
        with self.assertRaises(ValueError):
            leaving(TableColumn("Name", drop=1), TableColumn("Note"))

    def test_a_table_draws_no_more_columns_than_its_grid_has_tracks(self):
        columns = tuple(TableColumn(f"Column {at}") for at in range(TRACKS))
        acted = Row(tuple(Cell(column.label) for column in columns), actions=(PageAction("Open", "/"),))
        DataTable("Things", columns)
        for refused in (
            lambda: DataTable("Things", (*columns, TableColumn("Over"))),
            lambda: DataTable("Things", columns, (acted,)),
        ):
            with self.subTest(refused), self.assertRaises(ValueError):
                refused()

    def test_a_cell_held_back_holds_no_control(self):
        """A cell its row's open line copies holds no field, which the copy
        would draw twice: it stands in place, or moves there whole."""

        held = Cell(field=object(), parts=(Cell("since", shows="more"),))
        table = DataTable("Things", (TableColumn("Name"), TableColumn("Note")), (Row((Cell("A"), held)),))
        with self.assertRaisesMessage(ValueError, "holds no control"):
            drawn("{% data_table table %}", table=table)

    def test_a_row_editor_given_a_value_for_a_field_its_form_lacks_is_refused(self):
        class Distance(forms.Form):
            distance_miles = forms.DecimalField()

        editor = RowEditor(Distance(), "example-01", "Correct", "/example/distance/", "Save", {"miles": "3.10"})
        row = Row((Cell("example-01", name=True), Cell(editor=editor)))
        table = DataTable("Example runs", (TableColumn("Run"), TableColumn("Fix", kind="control")), (row,))
        request = RequestFactory().get("/example/runs/?edit=example-01")
        with self.assertRaisesMessage(TemplateSyntaxError, "miles"):
            drawn("{% data_table table %}", request, table=table)


class ValueRefusalTests(SimpleTestCase):
    def test_a_value_with_no_words_of_its_own_is_refused(self):
        with self.assertRaises(Unprintable):
            or_empty(type("Named", (), {"__str__": lambda self: "example-operator"})())

    def test_a_cell_line_is_one_line_or_its_own_count(self):
        with self.assertRaises(ValueError):
            cell_line("example-agent", lines=3)

    def test_a_count_of_decimals_that_is_not_a_number_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            drawn('{% load value_tags %}{{ x|measured:"mi,two" }}', x=1)
