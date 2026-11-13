"""Which network an address belongs to."""

from unittest import mock

from django.test import SimpleTestCase, TestCase

from hq.domains.control_plane.models import ManagedResource

from ..exposure import exposure_of_name
from ..projection import projection_scope
from .test_paths import PUBLIC_RANGE, record, store


class OnLinkNetworkTests(SimpleTestCase):
    def test_only_global_prefixes_on_the_hosts_own_interfaces_count(self):
        import tempfile
        from ipaddress import ip_network
        from pathlib import Path

        from ..reach import on_link_networks

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

        from ..reach import on_link_networks

        self.assertEqual(on_link_networks(Path("/nonexistent/if_inet6")), ())


class PublicLabelTests(SimpleTestCase):
    def test_ipv6_folds_to_its_64_and_non_addresses_are_skipped(self):
        from ..public_registry import public_endpoints
        from ..reach import public_label

        self.assertEqual(public_label("2001:db8:0:1::a1"), "2001:db8:0:1::/64")
        self.assertEqual(public_label("203.0.113.7"), "203.0.113.7")
        self.assertEqual(public_label("not-an-address"), "")
        with mock.patch("hq.platform.application.public_registry.holders", return_value={}):
            self.assertEqual(
                public_endpoints(("not-an-address", "2001:db8:0:1::a1", "2001:db8:0:1::b2")),
                (("2001:db8:0:1::/64", ""),),
            )


# The access list the controller reads off a proxy host (``NPMAccessPolicy``):
# the tailnet's two ranges allowed, everyone else refused.
TAILNET_LIST = {
    "name": "tailnet only",
    "satisfy_any": False,
    "pass_auth": False,
    "authorization_count": 0,
    "implicit_deny": True,
    "clients": [
        {"directive": "allow", "address": "100.64.0.0/10"},
        {"directive": "allow", "address": "fd7a:115c:a1e0::/48"},
    ],
}


def proxy_host(name, **access):
    return {
        "domain_names": [name],
        "forward_scheme": "http",
        "forward_host": "127.0.0.1",
        "forward_port": 8000,
        "connection_ref": "example-npm",
        **access,
    }


@PUBLIC_RANGE
class AccessListReachTests(TestCase):
    """Reach never names a wider audience than the proxy in front admits."""

    def setUp(self):
        ManagedResource.objects.create(key="lab-1", kind="machine", spec={"name": "lab-1", "addresses": ["10.0.0.5"]})
        ManagedResource.objects.create(
            key="edge-1", kind="machine", spec={"name": "edge-1", "addresses": ["198.51.100.20"]}
        )
        store(
            "adguard.rewrite", {"domain": "home.example.com", "answer": "10.0.0.5", "connection_ref": "example-adguard"}
        )
        store("cloudflare.dns_record", record("shop.example.com", "A", "198.51.100.20", proxied=False))

    def reach(self, hostname, *hosts):
        store("npm.proxy_host", *hosts)
        with projection_scope():
            return exposure_of_name(hostname).reach

    def test_a_tailnet_only_list_narrows_a_private_answer(self):
        found = self.reach(
            "home.example.com", proxy_host("home.example.com", access_list_id=1, access_policy=TAILNET_LIST)
        )

        self.assertEqual((found.id, found.label), ("tailnet", "Tailnet only"))
        # The detail says what narrowed it, never that the answer was a tailnet address.
        self.assertIn("access list", found.detail)

    def test_a_tailnet_only_list_narrows_a_public_answer(self):
        found = self.reach(
            "shop.example.com", proxy_host("shop.example.com", access_list_id=1, access_policy=TAILNET_LIST)
        )

        self.assertEqual((found.id, found.label), ("tailnet", "Tailnet only"))

    def test_without_a_read_list_reach_is_what_the_answer_says(self):
        from ..reach import LOCAL_NETWORK

        for case, host in (
            ("no list", proxy_host("home.example.com")),
            ("rules not read", proxy_host("home.example.com", access_list_id=1)),
            (
                "a password admits anyone",
                proxy_host(
                    "home.example.com",
                    access_list_id=1,
                    access_policy={**TAILNET_LIST, "satisfy_any": True, "authorization_count": 1},
                ),
            ),
        ):
            with self.subTest(case):
                self.assertEqual(self.reach("home.example.com", host), LOCAL_NETWORK)


class AdmittedNetworkTests(SimpleTestCase):
    def policy(self, rules, **fields):
        from hq.domains.control_plane.provider_adapters.contracts import IngressPolicy

        defaults = {"implicit_deny": False, "satisfy_any": False, "authorizations": 0}
        return IngressPolicy(("app.example.com",), restricted=True, rules=tuple(rules), **{**defaults, **fields})

    def test_the_widest_range_allowed_before_everyone_else_is_refused(self):
        from ..reach import admitted_network

        for rules, expected in (
            ((("allow", "100.64.0.0/10"), ("deny", "all")), "tailnet"),
            ((("allow", "100.64.0.7"), ("allow", "10.0.0.0/24"), ("deny", "all")), "network"),
            ((("allow", "100.64.0.0/10"), ("allow", "198.51.100.0/24"), ("deny", "all")), ""),
            ((("allow", "100.64.0.0/10"), ("allow", "all")), ""),
            # The first match decides: an allow after "deny all" admits nobody.
            ((("allow", "100.64.0.0/10"), ("deny", "all"), ("allow", "all")), "tailnet"),
            # Without a refusal of everyone else, everyone else gets in.
            ((("allow", "100.64.0.0/10"),), ""),
            # A list that admits nobody names no audience.
            ((("deny", "all"),), ""),
        ):
            with self.subTest(rules=rules):
                self.assertEqual(admitted_network(self.policy(rules)), expected)

    def test_an_implicit_refusal_counts_and_a_password_with_satisfy_any_opens_it(self):
        from ..reach import admitted_network

        tailnet = (("allow", "100.64.0.0/10"), ("allow", "fd7a:115c:a1e0::/48"))
        self.assertEqual(admitted_network(self.policy(tailnet, implicit_deny=True)), "tailnet")
        self.assertEqual(
            admitted_network(self.policy(tailnet, implicit_deny=True, satisfy_any=True, authorizations=2)), ""
        )
        self.assertEqual(
            admitted_network(self.policy(tailnet, implicit_deny=True, satisfy_any=True, authorizations=None)), ""
        )
