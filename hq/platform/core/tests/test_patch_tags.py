"""The code patch: a block to paste, with its Copy button and notes, is one part."""

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import elements, select, with_class


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class PatchTagTests(SimpleTestCase):
    def test_its_copy_button_copies_its_code(self):
        html = _render(
            '{% patch id="example" %}{{ code }}{% title %}Add to <code>compose.yaml</code>'
            "{% detail %}Then run it.{% notes %}<li>An example note</li>{% endpatch %}",
            code="services:\n  example: <b>escaped</b>",
        )

        (patch,) = select(html, "id", "example")
        (button,) = select(html, "data-copy-target")
        (code,) = elements(html, "pre")
        self.assertEqual(code.attrs["id"], button.attrs["data-copy-target"])
        self.assertIn("<b>escaped</b>", code.words)
        self.assertTrue(all(patch in element.ancestors for element in (button, code)))
        self.assertIn("compose.yaml", html)
        self.assertEqual([item.words for item in elements(html, "li")], ["An example note"])
        (notes,) = with_class(html, "stack-list")
        self.assertIn(patch, notes.ancestors)

    def test_with_no_code_its_notes_stand_alone(self):
        html = _render('{% patch id="example" %}{% notes %}<li>An example note</li>{% endpatch %}')

        self.assertEqual(select(html, "data-copy-target"), [])
        self.assertEqual(elements(html, "pre"), [])
        self.assertEqual([item.words for item in elements(html, "li")], ["An example note"])

    def test_a_patch_without_an_id_or_with_an_unknown_option_is_refused(self):
        for source in (
            "{% patch %}code{% endpatch %}",
            '{% patch id="example" css="example" %}code{% endpatch %}',
            '{% patch id="example" data_example="x" %}code{% endpatch %}',
        ):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                _render(source)
