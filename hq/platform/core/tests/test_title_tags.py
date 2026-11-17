"""Title meta: the facts on a page's title line are one part."""

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, with_class


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class TitleMetaTagTests(SimpleTestCase):
    def test_its_parts_are_a_lists_items_with_the_pages_hooks(self):
        html = _render(
            "{% title_meta data_record_status=state %}<li>Current</li><li><code>{{ host }}</code></li>{% endtitle_meta %}",
            state="current",
            host="example-host",
        )

        (meta,) = with_class(html, "page-title-meta")
        self.assertEqual(meta.tag, "ul")
        self.assertIn("dot-list", meta.classes)
        self.assertEqual(meta.attrs["data-record-status"], "current")
        self.assertEqual([item.words for item in elements(html, "li")], ["Current", "example-host"])

    def test_words_outside_a_part_are_refused(self):
        """Words between the parts would join the line with no dot before
        them, and a dot written by hand can open a line."""

        for parts in ("Online{% dot %}<code>example-host</code>", "<li>Online</li> since today", ""):
            with self.subTest(parts=parts), self.assertRaises(TemplateSyntaxError):
                _render(f"{{% title_meta %}}{parts}{{% endtitle_meta %}}")

    def test_an_option_it_does_not_take_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            Template('{% title_meta css="example" %}<li>A</li>{% endtitle_meta %}')
