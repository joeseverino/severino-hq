"""A browser check's selector that matches nothing fails where the check asks it."""

from django.test import SimpleTestCase


class _Page:
    """A page that holds what ``drawn`` counts, by selector."""

    def __init__(self, path, drawn):
        self.url = f"http://127.0.0.1{path}"
        self.drawn = drawn

    def evaluate(self, _script, queries):
        return [self.drawn.get(query, 0) for query in queries]


class SelectorHeldTests(SimpleTestCase):
    def test_a_selector_a_page_lacks_fails_naming_its_part_and_the_fix(self):
        from hq.platform.core.tests.browser.selectors import SELECTORS, Unmatched, css

        page = _Page("/action-items/", {".table-scroll": 1})
        with self.assertRaises(Unmatched) as raised:
            css(page, [SELECTORS["decisions"], SELECTORS["table_scroll"], 3])
        said = str(raised.exception)
        for named in ("SELECTORS['decisions']", "/action-items/", "partials/_work_queue.html"):
            self.assertIn(named, said)
        self.assertIn("optional=True", said)

    def test_an_optional_selector_is_held_only_on_its_parts_entry(self):
        from hq.platform.core.template_contract import parts
        from hq.platform.core.tests.browser.selectors import SELECTORS, Unmatched, css

        page = _Page("/part/table-region/", {})
        self.assertEqual(css(page, [SELECTORS["table_scroll"], "x"]), [".table-scroll", "x"])
        region = next(part for part in parts() if part.name == "table_region")
        with self.assertRaises(Unmatched):
            css(page, SELECTORS["table_scroll"], drawn=lambda: region)

    def test_a_selector_reaches_a_page_only_through_css(self):
        from hq.platform.core.tests.browser.selectors import SELECTORS

        with self.assertRaises(TypeError):
            format(SELECTORS["row_ask"])
