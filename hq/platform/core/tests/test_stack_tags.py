"""The stack list and the dot list: a plain list read down, and a run read
across, are each one part."""

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, with_class


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


class DotListTagTests(SimpleTestCase):
    def test_its_items_are_the_pages_in_one_run_with_no_separator_written(self):
        html = _render(
            '{% dot_list data_example="example" %}<li>An example link</li><li>{{ note }}</li>{% enddot_list %}',
            note="<b>escaped</b>",
        )

        (run,) = with_class(html, "dot-list")
        self.assertEqual(run.tag, "ul")
        self.assertEqual(run.attrs["data-example"], "example")
        self.assertNotIn("muted", run.classes)
        self.assertEqual([item.words for item in elements(html, "li")], ["An example link", "<b>escaped</b>"])
        self.assertTrue(all(run in item.ancestors for item in elements(html, "li")))
        self.assertNotIn("·", run.words)

    def test_muted_says_the_run_quietly(self):
        html = _render("{% dot_list muted=True %}<li>example.com</li>{% enddot_list %}")

        (run,) = with_class(html, "dot-list")
        self.assertIn("muted", run.classes)

    def test_muted_is_true_or_false_and_an_unknown_option_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            _render('{% dot_list muted="yes" %}<li>a</li>{% enddot_list %}')
        with self.assertRaises(TemplateSyntaxError):
            Template('{% dot_list css="example" %}{% enddot_list %}')

    def test_an_extension_may_write_it(self):
        from hq.platform.core.template_contract import describe

        self.assertIn("dot_list", describe()["tags"])
