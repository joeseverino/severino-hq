"""Tailnet posture findings, and port names on the policy page."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from hq.platform.application.markup_testing import texts
from hq.platform.core.testing import connection, declare, operator

from ..findings import findings
from ..inventory_testing import store
from ..security import Capability, Principal
from ..tailnet import TAILNET_KIND, WELL_KNOWN_PORTS, grant_ports

EVERYTHING = Principal("test", "operator", frozenset(Capability))


def policy(*, lock=None, **record):
    store("tailscale.policy", {"record": "policy", **record})
    if lock is not None:
        store("host.tailnet_lock", {"record": "lock", **lock})


def raised(rule):
    return findings(principal=EVERYTHING, rule=rule)["findings"]


class DeviceApprovalTests(TestCase):
    def setUp(self):
        connection("tailscale", "example-tailnet")

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


class PortNameTests(TestCase):
    def setUp(self):
        store(
            "tailscale.device",
            {"name": "example-host", "online": True, "tags": ["tag:server"], "addresses": ["100.64.0.5"]},
        )
        connection("ssh", "example-host", endpoint="100.64.0.5:7722")
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

        connection("adguard", "example-dns", controller_id="example-host", endpoint="http://127.0.0.1:3001")

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
            addresses=(),
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

    def test_the_policy_page_renders_the_port_as_a_chip_and_its_name_beside_it(self):
        policy(
            groups=[{"name": "group:admins", "members": ["someone@example.com"]}],
            grants=[{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:7722"]}],
        )
        operator(self.client)

        response = self.client.get(reverse("control_plane:tailnet"))

        self.assertIn("tcp:7722", texts(response, "code"))
        self.assertIn("tcp:7722 SSH", texts(response, "td"))


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

    def test_an_unparsable_document_leaves_the_lists_alone(self):
        from ..tailnet import policy as read_policy

        policy(document="{ not json", grants=[{"src": ["*"], "dst": ["*"]}])

        found = read_policy()

        self.assertEqual((len(found.grants), found.acls), (1, ()))


def _own(*addresses):
    return mock.patch(
        "hq.platform.application.hq_self.host_addresses",
        return_value=frozenset({"127.0.0.1", "::1", *addresses}),
    )


class GrantReachesOnlyLoopbackTests(TestCase):
    """A grant port where what HQ knows listens there binds only loopback."""

    RULE = "grant-reaches-only-loopback"
    GRANT = {"src": ["example-mac"], "dst": ["example-host"], "ip": ["tcp:443", "tcp:8000"]}
    TEST = {"src": "example-mac", "proto": "tcp", "accept": ["example-host:443", "example-host:8000"]}

    def setUp(self):
        import json

        connection("tailscale", "example-tailnet")
        declare("example-host", "machine", {"name": "example-host", "addresses": ["100.64.0.5"]})
        store(TAILNET_KIND, {"name": "example-host", "addresses": ["100.64.0.5"]})
        written = {**self.GRANT, "srcPosture": ["posture:latest"]}
        policy(
            hosts={"example-host": "100.64.0.5"},
            grants=[self.GRANT],
            tests=[self.TEST],
            document=json.dumps({"grants": [written], "tests": [self.TEST]}),
        )

    def containers(self, *runtimes):
        """Containers on the machine, each ``(name, runtime fields)``, as Portainer read them."""

        store(
            "portainer.container", *({"host": "example-host", "name": name, "state": "running"} for name, _ in runtimes)
        )
        store(
            "portainer.runtime",
            *(
                {"host": "example-host", "container": name, "network_mode": "bridge", **fields}
                for name, fields in runtimes
            ),
        )

    def proxy(self):
        """A container publishing 443 on every address, so that port is served."""

        return ("example-proxy", {"port_bindings": [{"container_port": "443/tcp", "host_ip": "", "host_port": "443"}]})

    def test_hqs_own_host_network_container_does_not_count_as_serving_it(self):
        from types import SimpleNamespace

        self.containers(self.proxy(), ("example-hq", {"network_mode": "host", "exposed_ports": [8000]}))

        with (
            _own("100.64.0.5"),
            mock.patch(
                "hq.platform.application.tailnet_loopback.own_container",
                return_value=SimpleNamespace(address="example-host:example-hq"),
            ),
        ):
            self.assertEqual(len(raised(self.RULE)), 1)


class FunnelSettingTests(TestCase):
    """The settings say which nodes may publish to the internet, once the reading says."""

    def rows(self, **record):
        from ..tailnet import policy as read_policy

        policy(grants=[{"src": ["*"], "dst": ["*"], "ip": ["*"]}], **record)
        return dict(read_policy().facts)

    def test_no_node_holding_funnel_says_it_is_off(self):
        self.assertEqual(self.rows(funnel=[])["Funnel"], "Off: no node holds the funnel attribute")

    def test_each_target_holding_funnel_is_named(self):
        self.assertEqual(
            self.rows(funnel=["autogroup:member", "tag:web"])["Funnel"], "Allowed for autogroup:member, tag:web"
        )

    def test_a_reading_that_does_not_say_shows_no_row(self):
        self.assertNotIn("Funnel", self.rows())
