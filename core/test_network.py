"""The strict Host parse a fail-closed boundary uses."""

from __future__ import annotations

from django.test import SimpleTestCase

from core.network import strict_host


class StrictHostTests(SimpleTestCase):
    def test_well_formed_values_name_their_host(self):
        cases = {
            "hq.example.com": "hq.example.com",
            "HQ.Example.com.": "hq.example.com",
            "hq.example.com:8443": "hq.example.com",
            "192.0.2.4:80": "192.0.2.4",
            "[2001:db8::1]": "2001:db8::1",
            "[2001:db8::1]:8443": "2001:db8::1",
            "2001:db8::1": "2001:db8::1",
        }
        for value, host in cases.items():
            with self.subTest(value=value):
                self.assertEqual(strict_host(value), host)

    def test_malformed_values_name_nothing(self):
        for value in ("[2001:db8::1", "[2001:db8::1]x", "[2001:db8::1]:", "hq.example.com:",
                      "hq.example.com:https", ""):
            with self.subTest(value=value):
                self.assertEqual(strict_host(value), "")
