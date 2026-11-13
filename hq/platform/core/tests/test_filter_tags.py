"""The filter bar: every query a page asks of what it lists is one part."""

import inspect
import re
from types import SimpleNamespace

from django.template import Context, Template, TemplateSyntaxError
from django.template.loader import render_to_string
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.markup import elements, select, with_class
from hq.platform.core.tests.test_part_tags import _catalog_templates
from hq.platform.core.tests.test_template_values import template_sources


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class FilterBarTagTests(SimpleTestCase):
    def test_it_is_a_query_in_a_search_landmark_sent_by_filter(self):
        html = _render('{% filter_bar %}<select name="year"><option>2026</option></select>{% endfilter_bar %}')

        (landmark,) = elements(html, "search")
        (bar,) = with_class(html, "filter-bar")
        self.assertIn(landmark, bar.ancestors)
        self.assertEqual(bar.tag, "form")
        self.assertEqual(bar.attrs["method"], "get")
        self.assertNotIn("action", bar.attrs)
        (control,) = elements(html, "select")
        self.assertIn(bar, control.ancestors)
        (button,) = elements(html, "button")
        self.assertEqual((button.words, button.attrs["type"]), ("Filter", "submit"))
        self.assertEqual(elements(html, "a"), [])
        self.assertEqual(select(html, "role", "status"), [])

    def test_narrowed_it_offers_the_way_back_and_says_what_shows(self):
        html = _render(
            '{% filter_bar filtered=narrowed clear=every shown=3 total=1200 %}<input type="search" name="q">'
            "{% summary %}1 needs a look.{% endfilter_bar %}",
            narrowed=True,
            every="/example/",
        )

        (clear,) = elements(html, "a")
        self.assertEqual((clear.words, clear.attrs["href"]), ("Clear filters", "/example/"))
        (summary,) = select(html, "role", "status")
        self.assertIn("filter-bar-summary", summary.classes)
        self.assertTrue(summary.words.startswith("3 of 1,200 shown."), summary.words)
        self.assertTrue(summary.words.endswith("1 needs a look."), summary.words)
        (shown,) = select(html, "data-filter-bar-shown")
        self.assertEqual(shown.words, "3")

    def test_what_it_holds_back_is_counted_beside_the_one_link_that_shows_it(self):
        html = _render(
            '{% filter_bar shown=93 total=170 held=held more="/example/?view=all" %}'
            '<input type="search">{% endfilter_bar %}',
            held=("30 readings", "47 records with no links"),
        )

        (summary,) = select(html, "role", "status")
        self.assertIn("Held back: 30 readings, 47 records with no links.", summary.words)
        (more,) = elements(html, "a")
        self.assertEqual((more.words, more.attrs["href"]), ("Show them", "/example/?view=all"))

    def test_a_count_with_no_total_and_a_hold_with_no_way_to_it_are_refused(self):
        for source in (
            '{% filter_bar shown=3 %}<input name="q">{% endfilter_bar %}',
            '{% filter_bar shown=3 total=12 held=held %}<input name="q">{% endfilter_bar %}',
        ):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                _render(source, held=("3 readings",))

    def test_its_button_and_clear_stand_apart_from_the_controls_they_send(self):
        html = _render(
            '{% filter_bar filtered=True clear="/example/" %}<input type="search" name="q">{% endfilter_bar %}'
        )

        (controls,) = with_class(html, "filter-bar-controls")
        (send,) = with_class(html, "filter-bar-send")
        (search,) = elements(html, "input")
        self.assertIn(controls, search.ancestors)
        for sent in (*elements(html, "button"), *elements(html, "a")):
            self.assertIn(send, sent.ancestors)

    def test_nothing_narrowed_offers_no_way_back(self):
        html = _render('{% filter_bar filtered=narrowed clear="/example/" %}<input name="q">{% endfilter_bar %}')

        self.assertEqual(elements(html, "a"), [])

    def test_narrowed_with_no_way_back_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            _render('{% filter_bar filtered=True %}<input name="q">{% endfilter_bar %}')

    def test_it_asks_another_page_in_its_own_words_with_the_pages_hooks(self):
        html = _render(
            '{% filter_bar verb="Check" action="/example/check/" css="example" data_fragment_target="example" %}'
            '<select name="to"></select>{% endfilter_bar %}'
        )

        (bar,) = with_class(html, "filter-bar")
        self.assertEqual(bar.attrs["action"], "/example/check/")
        self.assertEqual(bar.attrs["data-fragment-target"], "example")
        self.assertIn("example", bar.classes)
        (button,) = elements(html, "button")
        self.assertEqual(button.words, "Check")

    def test_an_unknown_option_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            Template('{% filter_bar label="Example" %}{% endfilter_bar %}')


class TableToolbarTests(SimpleTestCase):
    """A list's toolbar is the filter bar, never a second one."""

    def test_the_table_toolbar_is_the_bar_its_script_answers(self):
        table = SimpleNamespace(
            query="example",
            search_placeholder="Search examples",
            filters=(),
            toggles=(),
            sorts=(),
            active_count=1,
        )

        html = render_to_string("partials/_table_toolbar.html", {"table": table})

        (bar,) = with_class(html, "filter-bar")
        self.assertIn("data-table-toolbar", bar.attrs)
        (search,) = [e for e in elements(html, "input") if e.attrs.get("type") == "search"]
        self.assertIn(bar, search.ancestors)
        (clear,) = [e for e in elements(html, "a") if bar in e.ancestors]
        self.assertEqual(clear.attrs["href"], "?")


@tag("source")
class FilterBarCatalogTests(SimpleTestCase):
    def test_every_option_and_part_of_the_filter_bar_has_a_catalog_entry(self):
        """The browser survey reads the part through the catalog, so each
        option it takes, and its summary, is drawn there at least once."""

        from hq.platform.core.templatetags.filter_tags import register

        takes = register.tags["filter_bar"].__wrapped__
        options = [
            option
            for option, parameter in inspect.signature(takes).parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        ]
        entries = " ".join(t for name, t in _catalog_templates().items() if name.startswith("form/filter-bar"))
        opened = " ".join(re.findall(r"\{% filter_bar (.*?)%\}", entries))
        self.assertEqual([o for o in options if f"{o}=" not in opened], [], "add a form/filter-bar-* entry")
        self.assertIn("data_", opened)
        self.assertIn("{% summary %}", entries)


# What a filter bar's controls and summary may not say for themselves.
_BAR = re.compile(r"\{% filter_bar(?P<options>.*?)%\}(?P<body>.*?)\{% endfilter_bar %\}", re.DOTALL)
_COMMENT = re.compile(r"\{% comment %\}.*?\{% endcomment %\}", re.DOTALL)
_EMPTY_OPTION = re.compile(r"<option(?![^>]*\bvalue=)[^>]*>|<option[^>]*\bvalue=(?:\"\"|'')")
_A_COUNT = re.compile(r"\b(?:of|shown)\b")
_CONTROL = re.compile(r"\{% select |<select\b|_toggle_set\.html|table-filter-menu|table-toggle")
# Its count is its pager's, which the list's table draws under it.
_COUNTED_BY_ITS_PAGER = frozenset({"partials/_table_toolbar.html"})


def filter_bar_faults(name: str, source: str) -> list[str]:
    """What each ``{% filter_bar %}`` in ``source`` says for itself that the part says."""

    faults = []
    for bar in _BAR.finditer(_COMMENT.sub("", source)):
        content, _, summary = bar["body"].partition("{% summary %}")
        options = bar["options"]
        if _EMPTY_OPTION.search(content):
            faults.append(f"{name}: an empty option written by hand; give its select every=")
        if _A_COUNT.search(re.sub(r"\{.*?\}", "", summary, flags=re.DOTALL)):
            faults.append(f"{name}: a count written in the summary; give the bar shown= and total=")
        if "filtered=" in options and "shown=" not in options and name not in _COUNTED_BY_ITS_PAGER:
            faults.append(f"{name}: a bar that narrows says how many it shows; give it shown= and total=")
        search, control = re.search(r'type="search"', content), _CONTROL.search(content)
        if search and control and control.start() < search.start():
            faults.append(f"{name}: the search comes first, then the selects, then the toggles")
    return faults


class FilterBarFaultTests(SimpleTestCase):
    """What the check over every use refuses, and what it lets pass."""

    def test_a_bar_that_says_for_itself_what_the_part_says_is_refused(self):
        for source in (
            '{% filter_bar %}<select name="k"><option value="">All kinds</option></select>{% endfilter_bar %}',
            '{% filter_bar %}<input name="q">{% summary %}{{ n }} of {{ m }}{% endfilter_bar %}',
            '{% filter_bar filtered=x clear=y %}<input name="q">{% endfilter_bar %}',
            '{% filter_bar %}{% select "k" kinds %}<input type="search">{% endfilter_bar %}',
        ):
            with self.subTest(source=source):
                self.assertEqual(len(filter_bar_faults("example.html", source)), 1)

    def test_a_bar_through_the_part_passes(self):
        source = (
            '{% filter_bar filtered=x clear=y shown=n total=m %}<input type="search">'
            '{% select "k" kinds every="kind" %}{% summary %}{{ k|counted:"needs a look,need a look" }}.'
            "{% endfilter_bar %}"
        )

        self.assertEqual(filter_bar_faults("example.html", source), [])


@tag("source", "tree")
class FilterBarUseTests(SimpleTestCase):
    def test_every_filter_bar_says_its_count_and_its_empty_choice_through_the_part(self):
        """Every bar reads the same: the search first, an empty choice only
        as "Any" from every=, and the count, what is held back and the link
        to it in the part's words, so no page's bar says everything while
        its count shows a part."""

        faults = [fault for name, source in template_sources().items() for fault in filter_bar_faults(name, source)]

        self.assertEqual(faults, [], "docs/Design.md, One filter bar")
