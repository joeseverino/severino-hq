"""Tailnet posture findings, and port names on the policy page."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection

from ..findings import findings
from ..inventory_testing import store
from ..security import Capability, Principal
from ..tailnet import WELL_KNOWN_PORTS, grant_ports

EVERYTHING = Principal("test", "operator", frozenset(Capability))


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

    def test_the_tailnet_page_marks_the_setting_and_links_the_problem(self):
        from ..tailnet_context import tailnet_context

        policy(
            settings={"devicesApprovalOn": False},
            grants=[{"src": ["*"], "dst": ["*"]}],
            tags=[{"name": "tag:server", "owners": []}, {"name": "tag:spare", "owners": []}],
        )
        store("tailscale.device", {"name": "example-host", "tags": ["tag:server"]})

        found = tailnet_context(principal=EVERYTHING)

        marked = {item.label: item.problem_url for item in found.settings if item.problem_url}
        self.assertEqual(
            marked,
            {"New devices": reverse("control_plane:findings") + "?rule=devices-join-without-approval"},
        )
        self.assertEqual(
            {row.tag["name"]: row.unworn for row in found.tags},
            {"tag:server": False, "tag:spare": True},
        )

    def test_approval_on_or_unread_says_nothing(self):
        policy(settings={"devicesApprovalOn": True})
        self.assertEqual(raised("devices-join-without-approval"), [])
        policy(settings={})
        self.assertEqual(raised("devices-join-without-approval"), [])

    def test_tailnet_lock_is_approval_by_signature_so_it_says_nothing(self):
        """Under lock a new node is filtered out until a signing key vouches
        for it, and Tailscale will not turn device approval on beside it."""

        policy(settings={"devicesApprovalOn": False}, lock={"enabled": True, "trusted_keys": 2})
        self.assertEqual(raised("devices-join-without-approval"), [])
        policy(settings={"devicesApprovalOn": False}, lock={"enabled": False})
        self.assertEqual(len(raised("devices-join-without-approval")), 1)

    def test_it_reaches_the_action_items(self):
        from ..attention import infrastructure

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
            grants=[{"src": ["group:empty", "group:admins"], "dst": ["tag:server"], "ip": ["tcp:22"]}],
        )

        (finding,) = raised("empty-group-granted")

        self.assertEqual(finding["severity"], "neutral")
        self.assertEqual(finding["title"], "group:empty has no members but is still granted access")
        self.assertEqual(finding["evidence"], [{"label": "Empty group", "value": "group:empty"}])

    def test_a_shell_rule_counts_as_a_grant(self):
        policy(
            groups=[{"name": "group:ops", "members": []}],
            ssh_rules=[{"action": "check", "src": ["group:ops"], "dst": ["tag:server"], "users": ["root"]}],
        )

        self.assertEqual(len(raised("empty-group-granted")), 1)


class RefusedConnectionTests(TestCase):
    def test_a_credential_refusal_says_refused(self):
        from hq.domains.control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL

        tailnet_connection("example-cf", provider="cloudflare_api")
        store(
            "cloudflare.pages_project",
            reachable=False,
            refusal=CREDENTIAL_REFUSAL,
            error="Invalid API token",
        )

        (finding,) = raised("connection-not-answering")

        self.assertEqual(finding["title"], "example-cf: credential refused")
        self.assertIn("Invalid API token", finding["explanation"])
        self.assertIn({"label": "State", "value": "Refused"}, finding["evidence"])

    def test_an_unreachable_connection_still_says_not_answering(self):
        ProviderConnection.objects.create(
            connection_ref="example-ssh",
            controller_id="example-controller",
            provider="ssh",
            endpoint="192.0.2.9:22",
            reachable=False,
            probed=True,
            detail="Timed out",
            observed_at=timezone.now(),
        )

        (finding,) = raised("connection-not-answering")

        self.assertEqual(finding["title"], "example-ssh is not answering")


class PortNameTests(TestCase):
    def setUp(self):
        store(
            "tailscale.device",
            {"name": "example-host", "online": True, "tags": ["tag:server"], "addresses": ["100.64.0.5"]},
        )
        ProviderConnection.objects.create(
            connection_ref="example-host",
            controller_id="example-controller",
            provider="ssh",
            endpoint="100.64.0.5:7722",
            reachable=True,
            probed=True,
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

    def test_a_connection_to_a_service_names_its_port(self):
        from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS

        ProviderConnection.objects.create(
            connection_ref="example-dns",
            controller_id="example-host",
            provider="adguard",
            endpoint="http://127.0.0.1:3001",
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

        self.assertEqual(self.names("tcp:3001")["tcp:3001"], CONNECTION_LABELS["adguard"])
        self.assertEqual(self.names("tcp:3001", dst=("tag:other",))["tcp:3001"], "")

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

        from ..projection import projection_scope

        machine = SimpleNamespace(
            name="example-host",
            aliases=(),
            reached_by=(),
            opened_by=(),
            containers=(),
            runs_hq=runs_hq,
        )
        with projection_scope(seed={"hq.served_port": port}):
            (grant,) = grant_ports(({"src": ["*"], "dst": ["tag:server"], "ip": [entry]},), machines=[machine])
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


class TrustedNetworksWidthTests(TestCase):
    """The trusted range only matters where the tailnet policy lets anyone in."""

    def admits(self, reach):
        from types import SimpleNamespace

        from .. import tailnet

        machine = SimpleNamespace(runs_hq=True, addresses=("100.64.0.5",))
        device = SimpleNamespace(addresses=("100.64.0.5",), reach=reach)
        with (
            mock.patch("hq.platform.application.connections.machines_once", return_value=(machine,)),
            mock.patch("hq.platform.application.tailnet.devices", return_value={"hq-box": device}),
        ):
            return tailnet._policy_admits_only_named()

    def test_named_devices_only_means_the_width_exposes_nothing(self):
        self.assertTrue(self.admits({443: ("example-mac", "tag:admin"), 22: ("example-mac",)}))

    def test_a_port_open_to_anyone_keeps_the_finding(self):
        self.assertFalse(self.admits({443: ("example-mac",), 8080: ("*",)}))
        self.assertFalse(self.admits({443: ("autogroup:member",)}))

    def test_an_unknown_never_silences_it(self):
        self.assertFalse(self.admits({}))


class PolicyDocumentTests(TestCase):
    """What the summary counts comes from the whole policy, not only the lists
    a reading happened to extract."""

    def test_a_policy_written_as_acls_is_not_read_as_empty(self):
        import json

        from ..tailnet import policy as read_policy

        policy(
            document=json.dumps(
                {
                    "acls": [
                        {"action": "accept", "src": ["group:admins"], "dst": ["tag:server:22"]},
                        {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:web:443"]},
                    ],
                    "groups": {"group:admins": ["someone@example.com"]},
                    "tagOwners": {"tag:server": ["group:admins"], "tag:web": ["group:admins"]},
                }
            )
        )

        found = read_policy()

        self.assertEqual(len(found.acls), 2)
        self.assertEqual([group["name"] for group in found.groups], ["group:admins"])
        self.assertEqual([tag["name"] for tag in found.tags], ["tag:server", "tag:web"])

    def test_the_lists_a_reading_extracted_win_over_the_document(self):
        import json

        from ..tailnet import policy as read_policy

        policy(groups=[{"name": "group:read", "members": []}], document=json.dumps({"groups": {"group:document": []}}))

        self.assertEqual([group["name"] for group in read_policy().groups], ["group:read"])

    def test_an_unparseable_document_leaves_the_lists_alone(self):
        from ..tailnet import policy as read_policy

        policy(document="{ not json", grants=[{"src": ["*"], "dst": ["*"]}])

        found = read_policy()

        self.assertEqual((len(found.grants), found.acls), (1, ()))
