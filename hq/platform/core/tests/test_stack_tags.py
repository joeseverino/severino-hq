"""The stack list: a plain list read down is one part."""

import inspect
import re

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.markup import elements, with_class
from hq.platform.core.tests.test_part_tags import _catalog_templates


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


@tag("source")
class StackListCatalogTests(SimpleTestCase):
    def test_every_option_of_the_stack_list_has_a_catalog_entry(self):
        """The browser survey reads the part through the catalog, so each
        option it takes is drawn there at least once."""

        from hq.platform.core.templatetags.stack_tags import register

        takes = register.tags["stack_list"].__wrapped__
        options = [
            option
            for option, parameter in inspect.signature(takes).parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        ]
        entries = " ".join(t for name, t in _catalog_templates().items() if name.startswith("part/stack-list"))
        opened = " ".join(re.findall(r"\{% stack_list (.*?)%\}", entries))
        self.assertEqual([o for o in options if f"{o}=" not in opened], [], "add a part/stack-list-* entry")
        self.assertIn("data_", opened)
