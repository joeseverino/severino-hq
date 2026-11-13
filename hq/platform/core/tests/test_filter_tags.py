"""The filter bar: every query a page asks of what it lists is one part."""

import inspect
import re
from types import SimpleNamespace

from django.template import Context, Template, TemplateSyntaxError
from django.template.loader import render_to_string
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.markup import elements, select, with_class
from hq.platform.core.tests.test_part_tags import _catalog_templates


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
            '{% filter_bar filtered=narrowed clear=every %}<input type="search" name="q">'
            "{% summary %}<strong>3 of 12 records</strong>{% endfilter_bar %}",
            narrowed=True,
            every="/example/",
        )

        (clear,) = elements(html, "a")
        self.assertEqual((clear.words, clear.attrs["href"]), ("Clear filters", "/example/"))
        (summary,) = select(html, "role", "status")
        self.assertIn("filter-bar-summary", summary.classes)
        self.assertEqual(summary.words, "3 of 12 records")

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
