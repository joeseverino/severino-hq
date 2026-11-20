"""An icon names a symbol of static/img/icons.svg, or fails as it draws."""

from django.template import TemplateSyntaxError
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class IconSheetTests(SimpleTestCase):
    def test_an_icon_the_sheet_draws_no_symbol_for_fails_as_it_draws(self):
        self.assertIn("#caret", render_to_string("partials/_icon.html", {"name": "caret"}))
        with self.assertRaisesMessage(TemplateSyntaxError, "'example' is no symbol"):
            render_to_string("partials/_icon.html", {"name": "example"})
