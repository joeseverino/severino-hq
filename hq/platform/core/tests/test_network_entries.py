"""A configured network list keeps its networks and names each entry that is none."""

from django.test import SimpleTestCase, override_settings

from hq.platform.core.network import trusted_proxies


class ConfiguredNetworkTests(SimpleTestCase):
    @override_settings(SEVERINO_TRUSTED_PROXIES=["198.51.100.0/24", "198.51.100.0/33-typo"])
    def test_an_entry_that_is_no_network_is_left_out_and_said(self):
        with self.assertLogs("severino.network", "WARNING") as logged:
            self.assertEqual(trusted_proxies(), ["198.51.100.0/24"])

        (said,) = logged.output
        self.assertIn("198.51.100.0/33-typo", said)
