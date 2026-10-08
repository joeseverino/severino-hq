"""HQ's record of which devices reached it: private, throttled, bounded."""

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.hq import ARRIVAL_KIND

from .. import arrivals

LAPTOP = "100.64.0.20"
PROXY = "172.18.0.2"


def a_tailnet():
    ProviderInventory.objects.create(
        kind="tailscale.device",
        observed_at=timezone.now(),
        records=[
            {
                "name": "a-laptop",
                "addresses": [LAPTOP],
                "public_key": "test-key-laptop",
                "last_handshake": timezone.now().isoformat(),
                "relay": "ord",
            },
            {"name": "hq-host", "addresses": ["100.64.0.10"], "self": True},
        ],
    )


def a_request(address=LAPTOP, method="post", **extra):
    return getattr(RequestFactory(), method)(
        "/private/path/?query=planted-query",
        REMOTE_ADDR=PROXY,
        HTTP_X_FORWARDED_FOR=address,
        HTTP_USER_AGENT="planted-agent/1.0",
        HTTP_COOKIE="sessionid=planted-cookie",
        HTTP_REFERER="https://example.test/planted-referer",
        **extra,
    )


def stored():
    row = ProviderInventory.objects.filter(kind=ARRIVAL_KIND).first()
    return row.records if row is not None else []


@override_settings(SEVERINO_REQUEST_PATH_SECONDS=300, SEVERINO_TRUSTED_PROXIES=["172.18.0.0/16"])
class ArrivalTests(TestCase):
    def setUp(self):
        arrivals.forget()
        self.addCleanup(arrivals.forget)
        a_tailnet()
        self.clock = 1000.0
        ticking = mock.patch("hq.platform.application.arrivals.monotonic", side_effect=lambda: self.clock)
        ticking.start()
        self.addCleanup(ticking.stop)

    def test_an_arrival_names_the_device_and_how_it_came(self):
        arrivals.note(a_request())

        (record,) = stored()
        self.assertEqual(record["device"], "a-laptop")
        self.assertEqual(record["address"], LAPTOP)
        self.assertEqual(record["channel"], "tailnet")
        self.assertEqual(record["carried"], "relayed")
        self.assertEqual(record["forwarded_by"], PROXY)
        self.assertEqual(record["count"], 1)

    def test_nothing_the_request_carried_beyond_the_source_is_kept(self):
        arrivals.note(a_request())

        text = json.dumps(stored())
        for planted in ("planted-query", "private/path", "planted-agent", "planted-cookie", "planted-referer"):
            self.assertNotIn(planted, text)
        fields = set(OBSERVATIONS[ARRIVAL_KIND].record.model_fields)
        self.assertLessEqual(set(stored()[0]), fields)

    def test_the_schema_admits_nothing_else(self):
        kept, _refused = OBSERVATIONS[ARRIVAL_KIND].clean(
            [{"address": LAPTOP, "path": "/x", "user_agent": "y", "headers": {"a": "b"}}]
        )

        self.assertEqual(set(kept[0]), {"address"})

    def test_a_source_is_written_at_most_once_per_interval(self):
        arrivals.note(a_request())
        with mock.patch("hq.platform.application.arrivals._write") as write:
            for _ in range(5):
                self.clock += 10
                arrivals.note(a_request())
        write.assert_not_called()

        self.clock += 300
        arrivals.note(a_request())

        self.assertEqual(stored()[0]["count"], 7)

    def test_a_throttled_request_costs_no_query(self):
        arrivals.note(a_request())
        self.clock += 1

        with self.assertNumQueries(0):
            arrivals.note(a_request())

    def test_records_past_the_window_are_dropped_on_write_and_ignored_on_read(self):
        old = (timezone.now() - arrivals.WINDOW - timedelta(days=1)).isoformat()
        ProviderInventory.objects.create(
            kind=ARRIVAL_KIND,
            observed_at=timezone.now(),
            records=[{"address": "100.64.0.99", "last_seen": old, "window_start": old, "count": 3}],
        )
        self.assertEqual(arrivals.arrivals(ProviderInventory.objects.filter(kind=ARRIVAL_KIND)), {})

        arrivals.note(a_request())

        self.assertEqual([record["address"] for record in stored()], [LAPTOP])

    def test_the_count_restarts_with_a_new_window(self):
        old = (timezone.now() - arrivals.WINDOW - timedelta(hours=1)).isoformat()
        recent = timezone.now().isoformat()
        ProviderInventory.objects.create(
            kind=ARRIVAL_KIND,
            observed_at=timezone.now(),
            records=[
                {"address": LAPTOP, "device": "a-laptop", "last_seen": recent,
                 "window_start": old, "first_seen": old, "count": 40}
            ],
        )

        arrivals.note(a_request())

        self.assertEqual(stored()[0]["count"], 1)
        self.assertEqual(stored()[0]["first_seen"], old)

    def test_at_most_max_sources_are_kept(self):
        with mock.patch.object(arrivals, "MAX_SOURCES", 2):
            for index in range(4):
                arrivals.note(a_request(f"10.0.0.{index + 1}"))

        self.assertEqual(len(stored()), 2)

    def test_a_failed_write_never_fails_the_request(self):
        with mock.patch("hq.platform.application.arrivals._write", side_effect=RuntimeError("boom")):
            arrivals.note(a_request())

        self.assertEqual(stored(), [])

    @override_settings(SEVERINO_REQUEST_PATH_SECONDS=0)
    def test_zero_records_nothing(self):
        arrivals.note(a_request())

        self.assertEqual(stored(), [])

    def test_a_page_load_counts_but_never_writes(self):
        with CaptureQueriesContext(connection) as queries:
            arrivals.note(a_request(method="get"))
            arrivals.note(a_request(method="head"))

        self.assertEqual(stored(), [])
        self.assertFalse(
            [q for q in queries.captured_queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        )
        arrivals.note(a_request())
        self.assertEqual(stored()[0]["count"], 3)

    def test_the_middleware_counts_page_loads_and_writes_on_a_post(self):
        user = get_user_model().objects.create_user("someone", password="not-used-here")
        self.client.force_login(user)

        self.client.get(reverse("dashboard"), REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_FOR=LAPTOP)
        self.assertEqual(stored(), [])
        self.client.post(reverse("dashboard_glance"), REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_FOR=LAPTOP)

        self.assertEqual(stored()[0]["device"], "a-laptop")
        self.assertEqual(stored()[0]["count"], 2)

    def test_the_machine_page_says_when_the_device_last_reached_hq(self):
        ManagedResource.objects.create(
            key="a-laptop", kind="machine", spec={"name": "a-laptop", "addresses": [LAPTOP]}
        )
        arrivals.note(a_request())
        user = get_user_model().objects.create_user("someone", password="not-used-here")
        self.client.force_login(user)

        with override_settings(SEVERINO_REQUEST_PATH_SECONDS=0):
            response = self.client.get(
                reverse("control_plane:machine", kwargs={"name": "a-laptop"})
            )

        self.assertContains(response, "Last reached HQ")
        self.assertContains(response, "relayed over the tailnet, through the proxy at 172.18.0.2")

    def test_it_is_not_said_of_the_machine_hq_runs_on(self):
        """Its controller reaches HQ without a request, so its last request says nothing."""

        from hq.domains.control_plane.models import ProviderInventory

        ManagedResource.objects.create(
            key="a-laptop", kind="machine", spec={"name": "a-laptop", "addresses": [LAPTOP]}
        )
        arrivals.note(a_request())
        for row in ProviderInventory.objects.filter(kind="tailscale.device"):
            row.records = [{**record, "self": True} for record in row.records]
            row.save()
        user = get_user_model().objects.create_user("someone", password="not-used-here")
        self.client.force_login(user)

        with override_settings(SEVERINO_REQUEST_PATH_SECONDS=0):
            response = self.client.get(
                reverse("control_plane:machine", kwargs={"name": "a-laptop"})
            )

        self.assertContains(response, "This is HQ's own machine.")
        self.assertNotContains(response, "over the tailnet")
