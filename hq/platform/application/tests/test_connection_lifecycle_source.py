"""A connection's lifecycle is said in one module (file_rules holds every other to it), and its markers keep the
values already stored."""

from django.test import SimpleTestCase

from ..connection_lifecycle import UNREPORTED_FACT, UNREPORTED_NODE


class LifecycleMarkerTests(SimpleTestCase):
    def test_the_markers_keep_the_values_already_stored(self):
        self.assertEqual(UNREPORTED_FACT, "connection-unreported")
        self.assertEqual(UNREPORTED_NODE, "connection:unreported:")
