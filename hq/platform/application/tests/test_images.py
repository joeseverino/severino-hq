from django.test import SimpleTestCase

from ..images import affected


class AdvisoryTests(SimpleTestCase):
    def test_ranges_as_publishers_write_them(self):
        self.assertTrue(affected("7.10.0", [(">= 7.5.0 && < 7.15.2", "")]))
        self.assertFalse(affected("v2.5.0", [("v2.0.0 - v.2.4.0", "")]))
        self.assertTrue(affected("7.0.1", [("7.0.0, 7.0.1", "")]))
        self.assertTrue(affected("5.0", [("3.0.0 < 6.1.1", "")]))

    def test_what_cannot_be_read_is_not_known_rather_than_safe(self):
        self.assertIsNone(affected("7.15.4", [("All", "")]))
        self.assertIsNone(affected("latest", [("< 1.0", "")]))
        self.assertFalse(affected("7.15.4", [("All", ""), ("< 5.1.0", "5.1.0")]))
