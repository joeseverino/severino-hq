"""What the tailnet says of each machine, raised as findings every door reads."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource

from ..attention import infrastructure
from ..findings import findings
from ..inventory_testing import store
from ..machines import machine
from ..security import Capability, Principal

EVERYTHING = Principal("test", "operator", frozenset(Capability))


def devices(*records, lock=None):
    store("tailscale.device", *({"online": True, **record} for record in records))
    if lock is not None:
        store("host.tailnet_lock", {"record": "lock", **lock})


def raised(rule):
    return findings(principal=EVERYTHING, rule=rule)["findings"]


def expiring_in(days):
    return (timezone.now() + timedelta(days=days)).isoformat()


def page(name):
    return reverse("control_plane:machine", args=[name])


def steps_text(finding) -> str:
    return " ".join(" ".join((step["label"], step["command"], *step["notes"])) for step in finding["operator_steps"])


class KeyExpiryTests(TestCase):
    """A deadline with no symptom until it passes."""

    def test_an_expiry_inside_the_window_is_raised_with_its_day_and_what_to_do(self):
        devices({"name": "example-edge", "addresses": ["100.64.0.7"], "key_expires": expiring_in(30)})

        (finding,) = raised("tailnet-key-expiring")

        self.assertEqual(finding["subject"], "machine:example-edge")
        self.assertEqual(finding["severity"], "attention")
        self.assertIn("30 days", finding["title"])
        self.assertIn("expires on", finding["explanation"])
        self.assertIn("Sign in to Tailscale again on example-edge", steps_text(finding))
        self.assertIn("turn off key expiry", steps_text(finding))
        self.assertIn("https://login.tailscale.com/admin/machines/100.64.0.7", steps_text(finding))

    def test_one_far_away_is_not_raised_at_all(self):
        """A queue that is never empty is one nobody reads."""

        devices({"name": "example-edge", "key_expires": expiring_in(120)})

        self.assertEqual(raised("tailnet-key-expiring"), [])

    def test_one_close_enough_is_serious_rather_than_routine(self):
        devices({"name": "example-edge", "key_expires": expiring_in(3)})

        self.assertEqual([finding["severity"] for finding in raised("tailnet-key-expiring")], ["serious"])

    def test_one_already_past_says_the_machine_has_left(self):
        devices({"name": "example-edge", "key_expires": expiring_in(-2)})

        (finding,) = raised("tailnet-key-expiring")

        self.assertIn("has left the tailnet", finding["title"])
        self.assertIn("expired on", finding["explanation"])

    def test_expiry_disabled_is_silent_because_there_is_no_date(self):
        devices({"name": "example-server", "key_expires": ""})

        self.assertEqual(raised("tailnet-key-expiring"), [])


class DeviceStandingTests(TestCase):
    def test_a_device_waiting_for_approval_is_serious_and_says_where_to_approve_it(self):
        devices({"name": "example-host", "authorized": False, "addresses": ["100.64.0.8"]}, {"name": "example-other"})

        (finding,) = raised("tailnet-device-unapproved")

        self.assertEqual((finding["subject"], finding["severity"]), ("machine:example-host", "serious"))
        self.assertIn("admin console", steps_text(finding))
        self.assertIn("https://login.tailscale.com/admin/machines/100.64.0.8", steps_text(finding))

    def test_a_lock_error_quotes_tailscale_and_names_the_command_that_lists_keys(self):
        devices({"name": "example-host", "lock_error": "node key not signed"})

        (finding,) = raised("tailnet-lock-unsigned")

        self.assertIn("node key not signed", finding["explanation"])
        self.assertIn("tailscale lock status", [step["command"] for step in finding["operator_steps"]])
        self.assertIn("tailscale lock sign", steps_text(finding))

    def test_a_device_lock_keeps_out_is_raised_though_the_device_reading_leaves_it_out(self):
        devices({"name": "example-host"}, lock={"enabled": True, "locked_out": ["example-locked"]})

        (finding,) = raised("tailnet-device-locked-out")

        self.assertIn("example-locked", finding["title"])
        self.assertIn("tailscale lock status", [step["command"] for step in finding["operator_steps"]])

    def test_a_device_with_nothing_wrong_raises_nothing(self):
        devices({"name": "example-host", "client_version": "1.80.0", "key_expires": ""})

        for rule in (
            "tailnet-key-expiring",
            "tailnet-device-unapproved",
            "tailnet-lock-unsigned",
            "tailnet-client-update",
            "tailnet-routes-unapproved",
        ):
            with self.subTest(rule):
                self.assertEqual(raised(rule), [])


class ClientUpdateTests(TestCase):
    def test_an_update_names_the_version_other_devices_run(self):
        devices(
            {"name": "example-host", "update_available": True, "client_version": "1.102.3-t0abc"},
            {"name": "example-laptop", "client_version": "1.102.4-t0def"},
        )

        (finding,) = raised("tailnet-client-update")

        self.assertIn("1.102.3", finding["explanation"])
        self.assertIn("1.102.4", finding["explanation"])
        self.assertIn("tailscale update", [step["command"] for step in finding["operator_steps"]])

    def test_a_release_with_a_trailing_zero_is_the_same_release(self):
        devices(
            {"name": "example-host", "update_available": True, "client_version": "1.80"},
            {"name": "example-laptop", "client_version": "1.80.0"},
        )

        (finding,) = raised("tailnet-client-update")

        self.assertNotIn("1.80.0", finding["explanation"])

    def test_an_update_with_no_newer_device_says_what_runs(self):
        devices({"name": "example-host", "update_available": True, "client_version": "1.102.3"})

        (finding,) = raised("tailnet-client-update")

        self.assertIn("1.102.3", finding["explanation"])


class RouteApprovalTests(TestCase):
    def test_routes_offered_and_not_approved_offer_the_approval_on_the_machine_s_page(self):
        devices(
            {
                "name": "example-router",
                "advertised_routes": ["192.0.2.0/24", "198.51.100.0/24"],
                "enabled_routes": ["198.51.100.0/24"],
            }
        )

        (finding,) = raised("tailnet-routes-unapproved")

        self.assertIn("192.0.2.0/24", finding["explanation"])
        self.assertNotIn("198.51.100.0/24", finding["explanation"])
        self.assertNotIn("exit node", finding["explanation"])
        self.assertEqual([remedy["capability"] for remedy in finding["remedies"]], ["tailnet.routes.approve"])

    def test_an_exit_node_not_approved_says_it_cannot_be_used(self):
        devices(
            {
                "name": "example-router",
                "advertised_routes": ["0.0.0.0/0", "::/0"],
                "offers_exit_node": True,
                "exit_node_approved": False,
            }
        )

        (finding,) = raised("tailnet-routes-unapproved")

        self.assertIn("exit node", finding["explanation"])


class QueueTests(TestCase):
    def test_every_family_reaches_the_queue_under_the_name_the_machine_page_uses(self):
        """A device the tailnet calls by its owner's label is one HQ lists
        under its own name: the item is that machine's, its key and links alike."""

        ManagedResource.objects.create(
            key="example-laptop",
            kind="machine",
            spec={"name": "example-laptop", "addresses": ["100.64.0.5"]},
            conditions=[{"type": "Ready", "status": True}],
            observed_generation=1,
            last_observed_at=timezone.now(),
        )
        devices(
            {
                "name": "Someone’s Laptop",
                "addresses": ["100.64.0.5"],
                "update_available": True,
                "authorized": False,
                "lock_error": "node key not signed",
                "advertised_routes": ["192.0.2.0/24"],
                "key_expires": expiring_in(3),
            }
        )

        items = [item for item in infrastructure() if item.key.startswith("finding:tailnet-")]

        self.assertEqual(machine("example-laptop").aliases, ("Someone’s Laptop",))
        self.assertEqual(
            {item.key.split(":")[1] for item in items},
            {
                "tailnet-key-expiring",
                "tailnet-device-unapproved",
                "tailnet-lock-unsigned",
                "tailnet-client-update",
                "tailnet-routes-unapproved",
            },
        )
        for item in items:
            with self.subTest(item.key):
                self.assertTrue(item.key.endswith(":machine:example-laptop"))
                self.assertEqual(item.subject.url, page("example-laptop"))
                self.assertNotIn("Someone", item.title)
                self.assertTrue(item.workflow.steps)
