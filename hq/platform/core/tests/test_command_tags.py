"""Text to paste, with its Copy button and notes, is one part."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, select, with_class


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class CommandTagTests(SimpleTestCase):
    def test_its_copy_button_copies_its_code(self):
        html = _render(
            '{% command id="example" %}{{ code }}{% title %}Add to <code>compose.yaml</code>'
            "{% detail %}Then run it.{% notes %}<li>An example note</li>{% endcommand %}",
            code="services:\n  example: <b>escaped</b>",
        )

        (command,) = select(html, "id", "example")
        (button,) = select(html, "data-copy")
        (code,) = elements(html, "pre")
        self.assertIn("<b>escaped</b>", code.words)
        # app.js copies the <pre> of the nearest box around the button that holds one: the code's own.
        self.assertEqual(code.ancestors[-1], button.ancestors[-1])
        self.assertIn(command, button.ancestors)
        self.assertIn("compose.yaml", html)
        self.assertEqual([item.words for item in elements(html, "li")], ["An example note"])
        (notes,) = with_class(html, "stack-list")
        self.assertIn(command, notes.ancestors)

    def test_given_its_text_it_is_one_tag_with_no_end(self):
        html = _render("{% command text=line %}<p>After</p>", line="example-cli <b>status</b>")

        (button,) = select(html, "data-copy")
        (code,) = elements(html, "pre")
        self.assertIn("example-cli <b>status</b>", code.words)
        self.assertEqual(code.ancestors[-1], button.ancestors[-1])
        self.assertEqual([p.words for p in elements(html, "p")], ["After"])

    def test_with_no_code_its_notes_stand_alone(self):
        html = _render('{% command id="example" %}{% notes %}<li>An example note</li>{% endcommand %}')

        self.assertEqual(select(html, "data-copy"), [])
        self.assertEqual(elements(html, "pre"), [])
        self.assertEqual([item.words for item in elements(html, "li")], ["An example note"])
