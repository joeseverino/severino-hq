"""The stack list: a plain list read down is one part."""

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import elements, with_class


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class StackListTagTests(SimpleTestCase):
    def test_its_items_are_the_pages_with_the_pages_hooks(self):
        html = _render(
            '{% stack_list data_example="example" %}<li>An example note</li><li>{{ note }}</li>{% endstack_list %}',
            note="<b>escaped</b>",
        )

        (stack,) = with_class(html, "stack-list")
        self.assertEqual(stack.tag, "ul")
        self.assertEqual(stack.attrs["data-example"], "example")
        self.assertNotIn("row-below", stack.classes)
        self.assertEqual([item.words for item in elements(html, "li")], ["An example note", "<b>escaped</b>"])
        self.assertTrue(all(stack in item.ancestors for item in elements(html, "li")))

    def test_below_waits_for_the_open_row_under_its_label(self):
        html = _render('{% stack_list below="Affected" %}<li>example.com</li>{% endstack_list %}')

        (stack,) = with_class(html, "stack-list")
        self.assertIn("row-below", stack.classes)
        self.assertEqual(stack.attrs["data-label"], "Affected")

    def test_an_unknown_option_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            Template('{% stack_list css="example" %}{% endstack_list %}')
