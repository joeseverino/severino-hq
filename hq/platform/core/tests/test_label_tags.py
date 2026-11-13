"""The control label: a control's name is one part."""

import re

from django.template import Context, Template
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.markup import with_class
from hq.platform.core.tests.test_part_tags import _catalog_templates


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


@tag("source")
class ControlLabelCatalogTests(SimpleTestCase):
    def test_the_catalog_draws_a_control_label_with_and_without_a_control(self):
        """The browser survey reads the part through the catalog, so each form
        it draws is drawn there at least once."""

        drawn = re.findall(r"\{% control_label (.*?)%\}", " ".join(_catalog_templates().values()))
        self.assertTrue(any("control=" in tag for tag in drawn), "draw a label for a control")
        self.assertTrue(any("control=" not in tag for tag in drawn), "draw a label with no control")
