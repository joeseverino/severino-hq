"""What changed, when, and what happened near it."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderInventory
from core.models import AuditLog

from ..conditions import held_since, stamped
from ..inventory import READING_AUDIT_TYPE, record_inventory
from ..security import cli_principal
from ..timeline import moments, near
from ..topology_facts import add_observed_facts


def dns(content="192.0.2.10", name="app.example.com"):
    return {"zone": "example.com", "name": name, "record_type": "A", "content": content,
            "proxied": False, "ttl": 1, "connection_ref": "example-dns"}


def sweep(*records):
    record_inventory({"cloudflare.dns_record": {"ok": True, "records": list(records)}},
                     principal=cli_principal())


def readings():
    return list(AuditLog.objects.filter(object_type=READING_AUDIT_TYPE).values_list("message", flat=True))


class ConditionTests(TestCase):
    def test_a_condition_keeps_when_it_began_while_it_holds_the_same_way(self):
        start = timezone.now() - timedelta(days=2)
        first = stamped([], [{"type": "Drifted", "status": True, "message": "one"}], now=start)
        again = stamped(first, [{"type": "Drifted", "status": True, "message": "two"}])

        self.assertEqual(held_since(again, "Drifted"), start)

    def test_a_condition_that_stops_and_returns_starts_again(self):
        start = timezone.now() - timedelta(days=2)
        drifted = stamped([], [{"type": "Drifted", "status": True}], now=start)
        ready = stamped(drifted, [{"type": "Ready", "status": True}])
        back = stamped(ready, [{"type": "Drifted", "status": True}])

        self.assertGreater(held_since(back, "Drifted"), start)
        self.assertIsNone(held_since(ready, "Drifted"))


class ReadingChangeTests(TestCase):
    def test_a_record_that_changed_between_sweeps_is_a_moment(self):
        sweep(dns())
        sweep(dns(content="192.0.2.99"), dns(name="new.example.com"))

        self.assertEqual(readings(), ["Public DNS record changed: 2 records new or changed, 1 record gone"])

    def test_the_first_sweep_and_an_identical_one_are_not(self):
        sweep(dns())
        sweep(dns())

        self.assertEqual(readings(), [])

    def test_a_failed_read_is_not_a_change(self):
        sweep(dns())
        record_inventory({"cloudflare.dns_record": {"ok": False, "error": "refused"}}, principal=cli_principal())

        self.assertEqual(readings(), [])


class TimelineTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        ProviderInventory.objects.create(
            kind="github.repository", observed_at=self.now,
            records=[{"repository": "example/app", "connection_ref": "example-github", "deployments": [
                {"environment": "production", "sha": "abc1234def", "created_at": (self.now - timedelta(hours=1)).isoformat(),
                 "url": "https://github.com/example/app/actions/runs/1"},
            ]}],
        )
        ProviderInventory.objects.create(
            kind="portainer.runtime", observed_at=self.now,
            records=[{"container": "app", "host": "example-box", "connection_ref": "example-portainer",
                      "started_at": (self.now - timedelta(hours=2)).isoformat()}],
        )

    def test_deploys_starts_and_changes_are_one_line_newest_first(self):
        found = moments(since=self.now - timedelta(days=1))

        self.assertEqual([item.source for item in found], ["Deploy", "Container"])
        self.assertEqual(found[0].title, "Deployed abc1234 to production")
        self.assertEqual(found[1].title, "app started on example-box")

    def test_near_is_what_happened_closest_to_the_moment(self):
        (closest, *_rest) = near(self.now - timedelta(hours=2, minutes=5))

        self.assertEqual(closest.source, "Container")

    def test_a_drift_says_when_it_was_first_seen_and_what_happened_near_then(self):
        resource = ManagedResource.objects.create(
            key="example-record", kind="cloudflare.dns_record", spec={},
            conditions=stamped([], [{"type": "Drifted", "status": True, "message": "differs"}],
                               now=self.now - timedelta(hours=1, minutes=10)),
        )

        facts = dict(add_observed_facts((resource,))).get(f"resource:{resource.key}", ())

        self.assertIn("drift-since", [key for key, _value in facts])
        near_then = [value for key, value in facts if key == "drift-near"]
        self.assertTrue(near_then[0].startswith("Deploy: Deployed abc1234 to production"))


class TimelinePageTests(TestCase):
    def test_it_lists_the_window_and_asks_to_sign_in(self):
        url = reverse("control_plane:timeline")
        self.assertEqual(self.client.get(url).status_code, 302)

        self.client.force_login(get_user_model().objects.create_user("example-operator", password="x" * 20))
        response = self.client.get(url, {"days": "30"})

        self.assertContains(response, 'aria-current="page">30 days')
        self.assertContains(response, "Nothing happened in this window.")
