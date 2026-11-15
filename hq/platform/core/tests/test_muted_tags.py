from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import elements, select, with_class


def drawn(source: str, **context) -> str:
    return Template("{% load value_tags %}" + source).render(Context(context))


class MutedTests(SimpleTestCase):
    """Words in the muted voice, drawn by the one tag that writes them."""

    def test_the_words_are_drawn_in_the_muted_voice_as_a_line_or_a_paragraph(self):
        line = drawn("<p>3 checks {% muted %}since {{ when }}{% endmuted %}</p>", when="Monday")
        paragraph = drawn('{% muted element="p" %}A sentence under it.{% endmuted %}')

        self.assertEqual([part.tag for part in with_class(line, "muted")], ["span"])
        self.assertEqual(with_class(line, "muted")[0].words, "since Monday")
        self.assertEqual([part.tag for part in with_class(paragraph, "muted")], ["p"])

    def test_where_it_stands_adds_its_classes_and_attributes(self):
        shown = drawn(
            '{% muted css="row-more" title="Example" data_tip="Why" %}Example{% endmuted %}',
        )

        (part,) = with_class(shown, "muted")
        self.assertIn("row-more", part.classes)
        self.assertEqual(part.attrs.get("title"), "Example")
        self.assertEqual([found.attrs["data-tip"] for found in select(shown, "data-tip")], ["Why"])

    def test_the_words_are_escaped_as_the_page_writes_them(self):
        shown = drawn("{% muted %}{{ words }}{% endmuted %}", words="<b>x</b>")

        self.assertEqual(elements(shown, "b"), [])
        self.assertEqual(with_class(shown, "muted")[0].words, "<b>x</b>")

    def test_an_element_or_option_it_does_not_take_is_refused(self):
        for source in (
            '{% muted element="div" %}x{% endmuted %}',
            '{% muted tone="good" %}x{% endmuted %}',
        ):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                drawn(source)

    def test_an_extension_may_write_it(self):
        from hq_sdk.template_contract import TAGS

        self.assertIn("muted", TAGS)
