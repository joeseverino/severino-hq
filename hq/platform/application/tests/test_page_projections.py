"""The tailnet and connections pages render one projection that every adapter returns.

Parity is asserted on the object: the page's context holds the projection, and
the registered read serializes the same projection, so a fact on the page that
the API, MCP, CLI or SDK cannot return fails here.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory

from ..connection_context import ConnectionsContext, connections_context
from ..resources import (
    InvalidResourceInput,
    ResourceNotFound,
    get_resource,
    list_resource,
)
from ..security import AuthorizationError, Capability, Principal, cli_principal
from ..tailnet_context import TailnetContext

NOW = timezone.now()


def store(kind, records, **extra):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": records,
            "reachable": True,
            "connected": True,
            "observed_at": NOW,
            "controller_id": "example-controller",
            **extra,
        },
    )


def connect(ref, provider, endpoint):
    ProviderConnection.objects.update_or_create(
        controller_id="example-controller",
        connection_ref=ref,
        defaults={"provider": provider, "endpoint": endpoint, "observed_at": NOW},
    )


POLICY = {
    "record": "policy",
    "settings": {"devicesApprovalOn": False, "devicesKeyDurationDays": 90},
    "dns": {"dns": ["100.64.0.5"], "magicDNS": True},
    "hosts": {"example-host": "100.64.0.5"},
    "groups": [
        {"name": "group:admins", "members": ["someone@example.com"]},
        {"name": "group:empty", "members": []},
    ],
    "tags": [{"name": "tag:server", "owners": ["group:admins"]}],
    "grants": [
        {"src": ["group:admins"], "dst": ["example-host"], "ip": ["tcp:22"]},
        {"src": ["group:empty"], "dst": ["tag:server"], "ip": ["tcp:443"]},
    ],
    "tests": [{"src": "group:admins", "accept": ["example-host:22"]}],
    "ssh_rules": [
        {"src": ["group:admins"], "dst": ["tag:server"], "users": ["root"], "action": "check"}
    ],
    "app_connectors": [
        {"name": "example-connector", "connectors": ["tag:server"], "domains": ["example.org"]}
    ],
}
DEVICE = {
    "name": "example-host",
    "user": "someone@example.com",
    "tags": ["tag:server"],
    "addresses": ["100.64.0.5"],
    "online": True,
    "public_key": "example-public-key",
    "direct_endpoint": "198.51.100.7:41641",
    "last_handshake": (NOW - timedelta(minutes=1)).isoformat(),
    "rx_bytes": 2048,
    "tx_bytes": 1024,
}


def populate():
    store("tailscale.policy", [POLICY])
    store("tailscale.device", [DEVICE])
    store("tailscale.dns", [{"record": "dns", "nameservers": ["100.64.0.5"]}])
    # The credential reads devices but was refused users: a missing permission.
    store("tailscale.user", [], reachable=False, refusal="permission", error="Forbidden")
    connect("example-tailnet", "tailscale", "https://api.example.com")
    connect("example-host", "ssh", "100.64.0.5:22")


def without_request(payload):
    return {key: value for key, value in payload.items() if key != "request"}


class TailnetProjectionTests(TestCase):
    def setUp(self):
        populate()
        self.client.force_login(
            get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        )

    def test_the_page_and_the_read_are_one_projection(self):
        response = self.client.get(reverse("control_plane:tailnet"))

        found = response.context["tailnet"]
        self.assertIsInstance(found, TailnetContext)
        # Nothing derived beside it: the page renders the projection.
        for retired in ("policy", "grant_rows", "ssh_rows", "fact_rows", "tag_devices"):
            self.assertNotIn(retired, response.context)
        read = list_resource("tailnet", principal=cli_principal())
        self.assertEqual(read["count"], 1)
        self.assertEqual(found.as_dict(), read["items"][0])

    def test_the_read_carries_what_the_page_shows_and_what_it_does_not(self):
        (tailnet,) = list_resource("tailnet", principal=cli_principal())["items"]

        self.assertTrue(tailnet["known"])
        self.assertEqual(tailnet["counts"], {"grants": 2, "groups": 2, "tags": 1, "tests": 1})
        settings = {item["label"]: item for item in tailnet["settings"]}
        self.assertEqual(settings["New devices"]["value"], "Join without approval")
        self.assertEqual(
            settings["Resolves through"]["machines"][0]["link"]["label"], "example-host (100.64.0.5)"
        )
        self.assertEqual(tailnet["grants"][0]["ports"], [{"entry": "tcp:22", "name": "SSH"}])
        self.assertEqual(tailnet["grants"][0]["destinations"][0]["link"]["label"], "example-host")
        self.assertEqual(tailnet["ssh_rules"][0]["action"], "check")
        self.assertEqual(tailnet["tags"][0]["machines"][0]["label"], "example-host")
        self.assertEqual(tailnet["app_connectors"][0]["domains"], ["example.org"])
        self.assertEqual(tailnet["tests"][0]["accept"], ["example-host:22"])
        rules = {finding["rule"] for finding in tailnet["findings"]}
        self.assertIn("devices-join-without-approval", rules)
        self.assertIn("empty-group-granted", rules)
        (unread,) = [item for item in tailnet["unread"] if item["kind"] == "tailscale.user"]
        self.assertEqual(unread["refusal"], "permission")
        self.assertEqual(unread["remedy"], "Add users:read to see Tailnet user")

    def test_the_page_renders_the_projection(self):
        response = self.client.get(reverse("control_plane:tailnet"))

        self.assertContains(response, "Join without approval")
        self.assertContains(response, "group:empty")
        self.assertContains(response, "example-connector")

    def test_nothing_read_is_said_as_not_read(self):
        ProviderInventory.objects.filter(kind="tailscale.policy").delete()

        (tailnet,) = list_resource("tailnet", principal=cli_principal())["items"]
        response = self.client.get(reverse("control_plane:tailnet"))

        self.assertFalse(tailnet["known"])
        self.assertIn("tailscale.policy", {item["kind"] for item in tailnet["unread"]})
        self.assertContains(response, "Tailnet policy not read yet.")

    def test_a_principal_without_read_is_refused(self):
        nobody = Principal("nobody", "test", frozenset())

        with self.assertRaises(AuthorizationError):
            list_resource("tailnet", principal=nobody)

    def test_unknown_input_is_refused(self):
        with self.assertRaises(InvalidResourceInput):
            list_resource("tailnet", {"limit": 5}, principal=cli_principal())


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class ConnectionProjectionTests(TestCase):
    def setUp(self):
        populate()
        self.client.force_login(
            get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        )

    def test_the_page_and_the_read_are_one_projection(self):
        response = self.client.get(reverse("control_plane:connections"))

        found = response.context["connections"]
        self.assertIsInstance(found, ConnectionsContext)
        for retired in (
            "connection_groups",
            "sight_by_connection",
            "credential_fixes",
            "connection_posture",
            "hq_path",
            "oldest",
            "unlabelled",
        ):
            self.assertNotIn(retired, response.context)
        web = found.as_dict()
        read = list_resource("connection.standing", principal=cli_principal())
        # The estate is the same for every caller; the request half is the page's.
        self.assertEqual(without_request(web), without_request(read))
        self.assertIsNone(read["request"])
        self.assertEqual(
            {control["id"] for control in web["request"]["controls"]},
            {"network", "transport", "proxy"},
        )
        self.assertFalse(
            {"network", "transport", "proxy"}
            & {control["id"] for control in read["posture"]["controls"]}
        )

    def test_one_connection_reads_as_its_row(self):
        read = list_resource("connection.standing", principal=cli_principal())
        row = get_resource("connection.standing", "example-host", principal=cli_principal())

        self.assertEqual(
            row, next(item for item in read["items"] if item["connection_ref"] == "example-host")
        )
        with self.assertRaises(ResourceNotFound):
            get_resource("connection.standing", "no-such-connection", principal=cli_principal())

    def test_how_hq_reaches_each_connection(self):
        rows = {
            item["connection_ref"]: item
            for item in list_resource("connection.standing", principal=cli_principal())["items"]
        }

        ssh = rows["example-host"]["reach"]
        self.assertEqual(ssh["network"], "tailnet")
        self.assertEqual(ssh["machine"]["name"], "example-host")
        self.assertEqual(ssh["peer_path"], "direct")
        self.assertEqual(ssh["direct_endpoint"], "198.51.100.7:41641")
        self.assertEqual(ssh["summary"], "Reached over the tailnet at example-host")
        api = rows["example-tailnet"]["reach"]
        self.assertEqual(api["network"], "public")
        self.assertEqual(api["summary"], "Reached over the internet")
        self.assertIsNone(api["peer_path"])

    def test_what_more_scope_would_show(self):
        rows = {
            item["connection_ref"]: item
            for item in list_resource("connection.standing", principal=cli_principal())["items"]
        }

        self.assertEqual(
            rows["example-tailnet"]["would_also_see"],
            ["Would also see Tailnet user with users:read"],
        )
        sights = {item["kind"]: item for item in rows["example-tailnet"]["sight"]["sights"]}
        self.assertEqual(sights["tailscale.device"]["freshness"], "current")

    def test_the_page_renders_the_projection(self):
        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "Would also see Tailnet user with users:read")
        self.assertContains(response, "Reached over the internet")
        self.assertContains(response, "Read all now")
        self.assertContains(
            response, 'formaction="/infrastructure/connections/read/?connection_ref=example-host"'
        )

    def test_a_read_asked_for_shows_on_its_row(self):
        self.client.post(
            reverse("control_plane:read_now"), {"connection_ref": "example-tailnet"}
        )

        rows = {
            item["connection_ref"]: item
            for item in list_resource("connection.standing", principal=cli_principal())["items"]
        }
        self.assertIsNotNone(rows["example-tailnet"]["read_requested_at"])
        self.assertIsNone(rows["example-host"]["read_requested_at"])
        self.assertContains(self.client.get(reverse("control_plane:connections")), "Read asked")

    def test_a_reader_sees_no_read_now(self):
        reader = Principal("reader", "test", frozenset({Capability.READ}))

        found = connections_context(principal=reader)

        self.assertIsNone(found.read_all)
        self.assertTrue(all(row.read_now is None for row in found.rows))

    def test_unknown_input_is_refused(self):
        with self.assertRaises(InvalidResourceInput):
            list_resource("connection.standing", {"limit": 5}, principal=cli_principal())
