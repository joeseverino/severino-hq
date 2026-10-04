"""A perimeter reading proves something only when it tried something."""

from __future__ import annotations

from django.test import TestCase

from ..inventory_testing import store
from .test_tailnet_posture import raised, tailnet_connection
from ..topology_facts import perimeter_unchecked


def perimeter(**record):
    store(
        "host.perimeter",
        {
            "record": "perimeter",
            "connection_ref": "example-edge",
            "firewall_unit": "active",
            "public_addresses": ["203.0.113.5"],
            "ports_checked": [22, 80, 443],
            "answered_publicly": [],
            **record,
        },
    )


class PerimeterUncheckedTests(TestCase):
    def setUp(self):
        tailnet_connection("example-edge", provider="ssh")

    def test_an_empty_probe_set_is_not_a_pass(self):
        perimeter(ports_checked=[])

        (finding,) = raised("perimeter-unchecked")

        self.assertEqual(finding["severity"], "attention")
        self.assertEqual(
            finding["evidence"], [{"label": "Not checked", "value": "no port to try"}]
        )
        self.assertEqual(raised("perimeter-open"), [])

    def test_no_public_address_checked_nothing_either(self):
        perimeter(public_addresses=[])

        (finding,) = raised("perimeter-unchecked")

        self.assertEqual(
            finding["evidence"], [{"label": "Not checked", "value": "no public address"}]
        )

    def test_a_checked_perimeter_raises_nothing_when_shut(self):
        perimeter()

        self.assertEqual(raised("perimeter-unchecked"), [])
        self.assertEqual(raised("perimeter-open"), [])

    def test_a_port_that_answered_is_still_the_open_finding(self):
        perimeter(answered_publicly=[443])

        (finding,) = raised("perimeter-open")

        self.assertIn("443", finding["title"])
        self.assertEqual(raised("perimeter-unchecked"), [])

    def test_the_reason_is_derived_from_the_record(self):
        self.assertEqual(perimeter_unchecked({}), "no public address")
        self.assertEqual(
            perimeter_unchecked({"public_addresses": ["203.0.113.5"]}), "no port to try"
        )
        self.assertEqual(
            perimeter_unchecked(
                {"public_addresses": ["203.0.113.5"], "ports_checked": [22]}
            ),
            "",
        )
