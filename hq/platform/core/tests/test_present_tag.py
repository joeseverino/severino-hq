"""``{% present %}``: markup around a value, or the one mark for a missing one."""

from django.template import TemplateSyntaxError, engines
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import texts, with_class


def drawn(source: str, **context) -> str:
    return engines["django"].from_string(source).render(context)


LINKED = '{% present release %}<a href="/example/releases/">{{ release }}</a>{% endpresent %}'


class PresentTagTests(SimpleTestCase):
    def test_a_value_that_is_there_draws_what_the_tag_encloses(self):
        page = drawn(LINKED, release="v1.2.0")

        self.assertEqual(texts(page, "a"), ["v1.2.0"])
        self.assertEqual(with_class(page, "empty-value"), [])

    def test_a_missing_value_draws_the_mark_the_filter_and_the_tag_draw(self):
        mark = drawn("{{ nothing|or_empty }}", nothing=None)
        self.assertEqual(mark, drawn("{% empty_value %}"))
        for missing in (None, "", [], ()):
            with self.subTest(missing=missing):
                page = drawn(LINKED, release=missing)
                self.assertEqual(page, mark)
                self.assertEqual(texts(page, "a"), [])
        with self.subTest("a name the context does not hold"):
            self.assertEqual(drawn(LINKED), mark)

    def test_zero_and_false_are_values_and_show(self):
        source = "{% present runs %}<code>{{ runs }}</code>{% endpresent %}"
        for value in (0, False):
            with self.subTest(value=value):
                page = drawn(source, runs=value)
                self.assertEqual(texts(page, "code"), [str(value)])
                self.assertEqual(with_class(page, "empty-value"), [])

    def test_the_value_may_be_a_filtered_expression(self):
        source = "{% present expense|default:asset %}<code>{{ expense|default:asset }}</code>{% endpresent %}"

        self.assertEqual(texts(drawn(source, expense=None, asset="example-laptop"), "code"), ["example-laptop"])
        self.assertNotEqual(with_class(drawn(source, expense=None, asset=None), "empty-value"), [])

    def test_it_takes_exactly_one_value(self):
        for source in ("{% present %}x{% endpresent %}", "{% present a b %}x{% endpresent %}"):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                drawn(source)

    def test_the_filter_marks_an_empty_collection_as_missing_too(self):
        self.assertEqual(drawn("{{ names|or_empty }}", names=[]), drawn("{% empty_value %}"))
