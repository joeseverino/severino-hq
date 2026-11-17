"""The control label: a control's name is one part."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import with_class


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class ControlLabelTagTests(SimpleTestCase):
    def test_it_labels_the_control_it_names(self):
        html = _render('{% control_label "From" control="example-from" %}<select id="example-from"></select>')

        (label,) = with_class(html, "control-label")
        self.assertEqual(label.tag, "label")
        self.assertEqual(label.attrs["for"], "example-from")
        self.assertEqual(label.words, "From")

    def test_with_no_control_it_names_what_follows(self):
        html = _render("{% control_label words %}", words="<b>Receiving</b>")

        (label,) = with_class(html, "control-label")
        self.assertEqual(label.tag, "span")
        self.assertNotIn("for", label.attrs)
        self.assertEqual(label.words, "<b>Receiving</b>")
