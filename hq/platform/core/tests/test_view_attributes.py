"""A view that defines a name Django's generic views read is refused as it is declared (the test runner's hook)."""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.views.generic import TemplateView


class ViewAttributeTests(SimpleTestCase):
    def test_a_view_shadowing_djangos_content_type_is_refused(self):
        class Shadowing:
            @property
            def content_type(self):
                return "example"

        with self.assertRaisesMessage(ImproperlyConfigured, "content_type"):
            type("ExampleView", (Shadowing, TemplateView), {})
        type("ExampleView", (TemplateView,), {"template_name": "example.html"})
