"""A perimeter reading proves something only when it tried something."""

from django.test import TestCase

from hq.platform.core.testing import connection

from ..topology_facts import perimeter_unchecked


class PerimeterUncheckedTests(TestCase):
    def setUp(self):
        connection("ssh", "example-edge", endpoint="https://api.example.com")

    def test_the_reason_is_derived_from_the_record(self):
        self.assertEqual(perimeter_unchecked({}), "no public address")
        self.assertEqual(perimeter_unchecked({"public_addresses": ["203.0.113.5"]}), "no port to try")
        self.assertEqual(
            perimeter_unchecked({"public_addresses": ["203.0.113.5"], "ports_checked": [22]}),
            "",
        )
