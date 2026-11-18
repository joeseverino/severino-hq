"""The prose list: a list of sentences is one part."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, with_class


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class ProseListTagTests(SimpleTestCase):
    def test_its_items_are_the_pages_in_a_marked_list(self):
        html = _render(
            "{% prose_list %}<li>An example sentence.</li><li>{{ note }}</li>{% endprose_list %}",
            note="<b>escaped</b>",
        )

        (listed,) = with_class(html, "prose-list")
        self.assertEqual(listed.tag, "ul")
        self.assertEqual([item.words for item in elements(html, "li")], ["An example sentence.", "<b>escaped</b>"])
        self.assertTrue(all(listed in item.ancestors for item in elements(html, "li")))

    def test_numbered_draws_items_read_in_order(self):
        html = _render("{% prose_list numbered=True %}<li>First.</li><li>Second.</li>{% endprose_list %}")

        (listed,) = with_class(html, "prose-list")
        self.assertEqual(listed.tag, "ol")
