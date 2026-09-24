"""Which network an address belongs to."""

from __future__ import annotations

from django.test import SimpleTestCase


class OnLinkNetworkTests(SimpleTestCase):
    def test_only_global_prefixes_on_the_hosts_own_interfaces_count(self):
        import tempfile
        from ipaddress import ip_network
        from pathlib import Path

        from .reach import on_link_networks

        lines = "\n".join(
            (
                "20010db8000000010000000000000001 02 40 00 00     eth0",
                "fe80000000000000000000000000abcd 02 40 20 80     eth0",
                "fd7a115ca1e000000000000000000001 05 80 00 00 tailscale0",
                "20010db8000000020000000000000001 03 40 00 00  docker0",
                "00000000000000000000000000000001 01 80 10 80       lo",
            )
        )
        with tempfile.NamedTemporaryFile("w", suffix="if_inet6") as handle:
            handle.write(lines + "\n")
            handle.flush()
            found = on_link_networks(Path(handle.name))

        self.assertEqual(found, (ip_network("2001:db8:0:1::/64"),))

    def test_a_host_without_the_file_has_none(self):
        from pathlib import Path

        from .reach import on_link_networks

        self.assertEqual(on_link_networks(Path("/nonexistent/if_inet6")), ())
