from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from ..pages import PageMixin


class DeclaredTrailTests(SimpleTestCase):
    """``page_trail`` is every crumb above the page, and a record's own follows them."""

    def test_one_crumb_written_bare_is_refused_where_the_view_is_declared(self):
        for wrong in (("Things", "/example/things/"), "Things", (("Things",),), [("Things", "/example/things/")]):
            with self.subTest(wrong=wrong), self.assertRaises(ImproperlyConfigured):
                type("Wrong", (PageMixin,), {"page_trail": wrong})
