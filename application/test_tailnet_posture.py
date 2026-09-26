"""Tailnet posture findings, and port names on the policy page."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ProviderConnection, ProviderInventory

from .findings import findings
from .security import Capability, Principal
from .tailnet import WELL_KNOWN_PORTS, grant_ports

EVERYTHING = Principal("test", "operator", frozenset(Capability))


def store(kind, *records, **extra):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": True,
            "connected": True,
            "observed_at": timezone.now(),
            "controller_id": "example-controller",
            **extra,
        },
    )


def tailnet_connection(ref="example-tailnet", provider="tailscale"):
    return ProviderConnection.objects.create(
        connection_ref=ref,
        controller_id="example-controller",
        provider=provider,
        endpoint="https://api.example.com",
        reachable=True,
        probed=True,
        observed_at=timezone.now(),
    )


def policy(**record):
    store("tailscale.policy", {"record": "policy", **record})


def raised(rule):
    return findings(principal=EVERYTHING, rule=rule)["findings"]


class DeviceApprovalTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_approval_off_is_an_attention_finding(self):
        policy(settings={"devicesApprovalOn": False}, grants=[{"src": ["*"], "dst": ["*"]}])

        (finding,) = raised("devices-join-without-approval")

        self.assertEqual(finding["severity"], "attention")
        self.assertEqual(finding["title"], "New devices join the tailnet without approval")
        self.assertIn("auth key", finding["explanation"])

    def test_approval_on_or_unread_says_nothing(self):
        policy(settings={"devicesApprovalOn": True})
        self.assertEqual(raised("devices-join-without-approval"), [])
        policy(settings={})
        self.assertEqual(raised("devices-join-without-approval"), [])

    def test_it_reaches_the_action_items(self):
        from .attention import infrastructure

        policy(settings={"devicesApprovalOn": False})

        keys = [item.key for item in infrastructure()]

        self.assertTrue(any(key.startswith("finding:devices-join-without-approval") for key in keys))


class EmptyGroupTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_an_empty_group_a_grant_names_is_a_cleanup_hint(self):
        policy(
            groups=[
                {"name": "group:empty", "members": []},
                {"name": "group:admins", "members": ["someone@example.com"]},
                {"name": "group:unused", "members": []},
            ],
            grants=[{"src": ["group:empty", "group:admins"], "dst": ["tag:server"],
                     "ip": ["tcp:22"]}],
        )

        (finding,) = raised("empty-group-granted")

        self.assertEqual(finding["severity"], "neutral")
        self.assertEqual(
            finding["title"], "group:empty has no members but is still granted access"
        )
        self.assertEqual(
            finding["evidence"], [{"label": "Empty group", "value": "group:empty"}]
        )

    def test_a_shell_rule_counts_as_a_grant(self):
        policy(
            groups=[{"name": "group:ops", "members": []}],
            ssh_rules=[{"action": "check", "src": ["group:ops"], "dst": ["tag:server"],
                        "users": ["root"]}],
        )

        self.assertEqual(len(raised("empty-group-granted")), 1)


class RefusedConnectionTests(TestCase):
    def test_a_credential_refusal_says_refused(self):
        from control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL

        tailnet_connection("example-cf", provider="cloudflare_api")
        store(
            "cloudflare.pages_project", reachable=False, refusal=CREDENTIAL_REFUSAL,
            error="Invalid API token",
        )

        (finding,) = raised("connection-not-answering")

        self.assertEqual(finding["title"], "example-cf's credential is refused")
        self.assertIn("Invalid API token", finding["explanation"])
        self.assertIn({"label": "State", "value": "Refused"}, finding["evidence"])

    def test_an_unreachable_connection_still_says_not_answering(self):
        ProviderConnection.objects.create(
            connection_ref="example-ssh", controller_id="example-controller",
            provider="ssh", endpoint="192.0.2.9:22", reachable=False, probed=True,
            detail="Timed out", observed_at=timezone.now(),
        )

        (finding,) = raised("connection-not-answering")

        self.assertEqual(finding["title"], "example-ssh is not answering")


class PortNameTests(TestCase):
    def setUp(self):
        store(
            "tailscale.device",
            {"name": "example-host", "online": True, "tags": ["tag:server"],
             "addresses": ["100.64.0.5"]},
        )
        ProviderConnection.objects.create(
            connection_ref="example-host", controller_id="example-controller",
            provider="ssh", endpoint="100.64.0.5:7722", reachable=True, probed=True,
            observed_at=timezone.now(),
        )
        store(
            "portainer.container",
            {"host": "example-host", "name": "example-admin", "ports": [9000]},
        )

    def names(self, *entries, dst=("tag:server",)):
        (grant,) = grant_ports(({"src": ["*"], "dst": list(dst), "ip": list(entries)},))
        return dict(grant["ports"])

    def test_the_ssh_connection_names_its_port(self):
        self.assertEqual(self.names("tcp:7722")["tcp:7722"], "SSH")

    def test_a_published_container_names_its_port(self):
        self.assertEqual(self.names("tcp:9000")["tcp:9000"], "example-admin")

    def test_well_known_ports_fall_back_to_the_registry(self):
        found = self.names("tcp:443", "53", "udp:53")

        self.assertEqual(found, {"tcp:443": "HTTPS", "53": "DNS", "udp:53": "DNS"})

    def test_an_unknown_port_or_range_stays_unnamed(self):
        found = self.names("tcp:8123", "tcp:80-90", "*")

        self.assertEqual(set(found.values()), {""})
        self.assertNotIn(9000, WELL_KNOWN_PORTS)

    def test_a_port_on_another_destination_is_not_named_by_this_one(self):
        self.assertEqual(self.names("tcp:7722", dst=("tag:other",))["tcp:7722"], "")

    def _hq_names(self, port, *, runs_hq=True, entry="tcp:8000"):
        from types import SimpleNamespace

        from .projection import projection_scope

        machine = SimpleNamespace(
            name="example-host", aliases=(), reached_by=(), opened_by=(),
            containers=(), runs_hq=runs_hq,
        )
        with projection_scope(seed={"hq.served_port": port}):
            (grant,) = grant_ports(
                ({"src": ["*"], "dst": ["tag:server"], "ip": [entry]},), machines=[machine]
            )
        return dict(grant["ports"])[entry]

    def test_hq_names_the_port_it_serves_on_its_own_machine(self):
        self.assertEqual(self._hq_names(8000), "Severino HQ")

    def test_the_same_port_elsewhere_is_not_hq(self):
        self.assertEqual(self._hq_names(8000, runs_hq=False), "")

    def test_without_a_request_port_hq_names_nothing(self):
        self.assertEqual(self._hq_names(None), "")

    def test_the_policy_page_renders_the_name_in_the_chip(self):
        policy(
            groups=[{"name": "group:admins", "members": ["someone@example.com"]}],
            grants=[{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:7722"]}],
        )
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)

        response = self.client.get(reverse("control_plane:tailnet"))

        self.assertContains(response, "<code>tcp:7722 · SSH</code>", html=False)
