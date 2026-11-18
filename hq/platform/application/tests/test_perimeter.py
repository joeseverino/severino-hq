"""A perimeter reading proves something only when it tried something."""

from django.test import TestCase

from ..topology_facts import perimeter_unchecked
from .test_tailnet_posture import tailnet_connection


class PerimeterUncheckedTests(TestCase):
    def setUp(self):
        tailnet_connection("example-edge", provider="ssh")

    def test_the_reason_is_derived_from_the_record(self):
        self.assertEqual(perimeter_unchecked({}), "no public address")
        self.assertEqual(perimeter_unchecked({"public_addresses": ["203.0.113.5"]}), "no port to try")
        self.assertEqual(
            perimeter_unchecked({"public_addresses": ["203.0.113.5"], "ports_checked": [22]}),
            "",
        )
