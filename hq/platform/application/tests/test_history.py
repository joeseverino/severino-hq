"""What changed, when, and what happened near it: one history, on the audit log."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.platform.core.models import AuditLog

from ..conditions import held_since, stamped
from ..history import moments, near
from ..inventory import READING_AUDIT_TYPE, record_inventory
from ..security import cli_principal
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
        record_inventory({"cloudflare.dns_record": {"ok": False, "records": [], "error": "refused"}}, principal=cli_principal())

        self.assertEqual(readings(), [])


class HistoryTests(TestCase):
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
        # A start leads to the container: its row on its machine's page.
        self.assertEqual(found[1].url, "/infrastructure/machines/example-box/#container-app")
        self.assertFalse(found[1].external)

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
        self.assertTrue(near_then[0].startswith("Deployed abc1234 to production"))


def event(action="created", *, ago, repr_="", type_="Container", message="", **extra):
    return AuditLog.objects.create(
        action=action, object_type=type_, object_repr=repr_, message=message,
        created_at=timezone.now() - ago, **extra,
    )


class AuditHistoryPageTests(TestCase):
    """The audit log is the one history: what HQ did, and what changed outside it."""

    url = staticmethod(lambda: reverse("core:audit_list"))

    def setUp(self):
        self.now = timezone.now()
        self.client.force_login(get_user_model().objects.create_user("example-operator", password="x" * 20))
        AuditLog.objects.all().delete()

    def stored(self):
        ProviderInventory.objects.create(
            kind="github.repository", observed_at=self.now,
            records=[{"repository": "example/app", "connection_ref": "example-github", "deployments": [
                {"environment": "production", "sha": "abc1234def",
                 "created_at": (self.now - timedelta(hours=1)).isoformat(),
                 "url": "https://github.com/example/app/actions/runs/1"},
            ]}],
        )
        ProviderInventory.objects.create(
            kind="portainer.runtime", observed_at=self.now,
            records=[{"container": "app", "host": "example-box", "connection_ref": "example-portainer",
                      "started_at": (self.now - timedelta(hours=2)).isoformat()}],
        )

    def rows(self, **params):
        return list(self.client.get(self.url(), params).context["object_list"])

    def test_signing_in_is_required(self):
        self.client.logout()

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_an_empty_log_says_so(self):
        response = self.client.get(self.url())

        self.assertContains(response, "Nothing has been recorded yet.")

    def test_everything_is_on_one_line_newest_first(self):
        self.stored()
        event("updated", ago=timedelta(minutes=30), repr_="example-record", type_="DNS record")
        event("observed", ago=timedelta(hours=3), type_=READING_AUDIT_TYPE,
              message="Public DNS record changed: 1 record gone")

        found = self.rows()

        self.assertEqual(
            [row.moment.source if row.moment else row.event.object_type for row in found],
            ["DNS record", "Deploy", "Container", READING_AUDIT_TYPE],
        )
        page = self.client.get(self.url()).content.decode()
        self.assertIn("Deployed abc1234 to production", page)
        self.assertIn('href="https://github.com/example/app/actions/runs/1"', page)
        self.assertIn(reverse("core:audit_detail", args=[found[0].event.pk]), page)

    def test_each_source_filters_to_itself(self):
        self.stored()
        event("updated", ago=timedelta(minutes=30), repr_="example-record", type_="DNS record")
        event("observed", ago=timedelta(hours=3), type_=READING_AUDIT_TYPE, message="changed")

        def sources(value):
            return [row.moment.source if row.moment else row.event.object_type
                    for row in self.rows(source=value)]

        self.assertEqual(sources("hq"), ["DNS record"])
        self.assertEqual(sources("outside"), [READING_AUDIT_TYPE])
        self.assertEqual(sources("deploy"), ["Deploy"])
        self.assertEqual(sources("container"), ["Container"])
        self.assertContains(self.client.get(self.url()), 'value="outside"')

    def test_a_run_of_like_events_is_one_line_with_its_records_behind_it(self):
        for index in range(14):
            event(ago=timedelta(minutes=10, seconds=index * 20), repr_=f"container-{index}")
        for index in range(3):
            event("updated", ago=timedelta(hours=2, seconds=index), repr_=f"record-{index}", type_="DNS record")
        event(ago=timedelta(days=1), repr_="lonely")

        found = self.rows()
        page = self.client.get(self.url()).content.decode()

        self.assertEqual([len(row.events) for row in found], [14, 3, 1])
        self.assertIn("<summary>14 containers</summary>", page)
        self.assertIn("<summary>3 DNS records</summary>", page)
        self.assertIn('<details class="history-run">', page)
        self.assertIn(reverse("core:audit_detail", args=[found[0].events[5].pk]), page)

    def test_a_run_is_counted_in_the_plural_its_type_has(self):
        """Not the label with an "s": that read "2 calendar entrys"."""

        for index in range(2):
            event(ago=timedelta(minutes=10, seconds=index * 20), repr_=f"entry-{index}", type_="Calendar entry")
        for index in range(2):
            event("updated", ago=timedelta(hours=2, seconds=index), repr_=f"sweep-{index}",
                  type_="ProviderInventory")

        page = self.client.get(self.url()).content.decode()

        self.assertIn("<summary>2 calendar entries</summary>", page)
        self.assertNotIn("entrys", page)
        # A model that declares its plural is counted in it.
        self.assertIn("<summary>2 provider inventories</summary>", page)

    def test_the_same_thing_again_and_again_is_counted_as_times(self):
        for index in range(4):
            event("login", ago=timedelta(minutes=10, seconds=index * 20), repr_="example-operator", type_="User")

        page = self.client.get(self.url()).content.decode()

        self.assertIn("<summary>example-operator · 4 times</summary>", page)
        self.assertNotIn("4 users", page)

    def test_a_run_breaks_on_another_actor_a_gap_or_a_moment_between(self):
        user = get_user_model().objects.get(username="example-operator")
        event(ago=timedelta(minutes=1), repr_="a")
        event(ago=timedelta(minutes=2), repr_="b", user=user)
        event(ago=timedelta(minutes=30), repr_="c")
        event(ago=timedelta(minutes=31), repr_="d")

        self.assertEqual([len(row.events) for row in self.rows()], [1, 1, 2])

    def test_a_search_or_the_approval_queue_lists_rows_one_by_one(self):
        self.stored()
        for index in range(3):
            event(ago=timedelta(minutes=index), repr_=f"container-{index}")

        self.assertEqual([len(row.events) for row in self.rows(action="created")], [1, 1, 1])
        self.assertEqual(self.rows(awaiting="1"), [])
        self.assertEqual(self.client.get(self.url(), {"awaiting": "1"}).status_code, 200)

    def test_a_moment_lands_on_the_one_page_its_time_falls_on(self):
        self.stored()
        for index in range(60):
            event(ago=timedelta(minutes=30 + index * 6), repr_=f"r{index}", type_=f"Type{index}")

        first, second = self.rows(), self.rows(page="2")
        moments_on = [[row.moment.source for row in rows if row.moment] for rows in (first, second)]

        self.assertEqual(moments_on, [["Deploy", "Container"], []])

    def test_the_page_costs_the_same_queries_however_much_history_there_is(self):
        self.stored()

        def cost():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(self.url()).status_code, 200)
            return len(queries)

        for index in range(5):
            event(ago=timedelta(minutes=index), repr_=f"a{index}", type_=f"Kind{index}")
        few = cost()
        for index in range(120):
            event(ago=timedelta(minutes=10 + index), repr_=f"b{index}", type_=f"Other{index}")
        self.assertEqual(cost(), few)


class NoSecondHistoryTests(TestCase):
    def test_the_separate_timeline_page_is_gone(self):
        self.client.force_login(get_user_model().objects.create_user("example-operator", password="x" * 20))

        self.assertEqual(self.client.get("/infrastructure/timeline/").status_code, 404)
        with self.assertRaises(NoReverseMatch):
            reverse("control_plane:timeline")

    def test_no_navigation_links_to_it(self):
        self.client.force_login(get_user_model().objects.create_user("example-operator", password="x" * 20))

        for url in (reverse("core:audit_list"), reverse("control_plane:findings")):
            page = self.client.get(url).content.decode()
            self.assertNotIn("/infrastructure/timeline/", page)
            self.assertIn(reverse("core:audit_list"), page)
