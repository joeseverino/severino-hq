"""What a reading has to survive between Cloudflare and a page.

The shapes here are the ones the live API actually returns (verified against
it) with synthetic values, because this repository is public and a real site
tag and its traffic are not things to publish alongside the code that reads
them.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from hq.domains.analytics.models import AnalyticsCoverage, AnalyticsSite, RumDaily, VitalsDaily
from hq.domains.content.models import ContentItem
from hq.platform.application import analytics as service
from hq.platform.application.resources import InvalidResourceInput, list_resource
from hq.platform.application.security import AuthorizationError, Capability, Principal
from hq.platform.application.tests.markup import hrefs, text

SITE_TAG = "0" * 32
HOST = "example.test"


def _principal() -> Principal:
    return Principal(
        actor="controller",
        interface="cli",
        capabilities=frozenset({Capability.MANAGE_INFRASTRUCTURE, Capability.READ}),
    )


def _day(offset: int) -> str:
    return (timezone.now().date() - timedelta(days=offset + 1)).isoformat()


def _payload(rows=None, vitals=None) -> dict:
    rows = rows if rows is not None else []
    vitals = vitals if vitals is not None else []
    dates = [item["date"] for item in (*rows, *vitals) if item.get("date")]
    start = min(dates, default=_day(0))
    end = max(dates, default=_day(0))
    return {
        "sites": [
            {
                "site_tag": SITE_TAG,
                "host": HOST,
                "connection_ref": "example-api",
                "start": start,
                "end": end,
                "rows": rows,
                "vitals": vitals,
            }
        ]
    }


def _site_payload(*, site_tag=SITE_TAG, host=HOST, connection_ref="example-api", rows=None) -> dict:
    rows = rows if rows is not None else []
    dates = [item["date"] for item in rows if item.get("date")]
    return {
        "site_tag": site_tag,
        "host": host,
        "connection_ref": connection_ref,
        "start": min(dates, default=_day(0)),
        "end": max(dates, default=_day(0)),
        "rows": rows,
        "vitals": [],
    }


def _row(dimension="path", value="/about/", offset=0, pageviews=20, visits=10):
    return {
        "dimension": dimension,
        "value": value,
        "date": _day(offset),
        "pageviews": pageviews,
        "visits": visits,
        "sample_interval": 10,
    }


A_DAY_OF_VITALS = {
    "sample_interval": 1,
    "cumulative_layout_shift": 0.05,
    "largest_contentful_paint_ms": 1800,
    "interaction_to_next_paint_ms": 120,
    "first_contentful_paint_ms": 900,
    "time_to_first_byte_ms": 200,
    **{f"{vital}_{bucket}": 1 for vital in ("lcp", "inp", "cls") for bucket in ("good", "needs_improvement", "poor")},
}


class IngestTests(TestCase):
    """A report is stored in one statement per table, however many rows it has."""

    def ingest(self, rows, vitals=None):
        with CaptureQueriesContext(connection) as captured:
            result = service.record_analytics(_payload(rows, vitals), principal=_principal())
        return result, [query["sql"] for query in captured.captured_queries]

    def test_its_statements_do_not_grow_with_the_rows(self):
        tables = {AnalyticsSite, AnalyticsCoverage, RumDaily, VitalsDaily}
        counts = {}
        for size in (50, 300):
            rows = [_row(value=f"/page-{index}/") for index in range(size)]
            vitals = [{"date": _day(0), **A_DAY_OF_VITALS}]
            self.ingest(rows, vitals)
            _result, restated = self.ingest(rows, vitals)
            counts[size] = len(restated)
            written = [sql for sql in restated if sql.startswith(("INSERT", "UPDATE"))]
            self.assertLessEqual(len(written), len(tables), written)
            self.assertEqual(RumDaily.objects.count(), size)
            RumDaily.objects.all().delete()

        self.assertEqual(counts[50], counts[300])

    def test_a_restated_grain_keeps_its_identity_and_takes_what_was_last_said(self):
        """A replayed sweep restates a day, a later one corrects it, and a grain
        said twice in one report is stored as said last."""

        self.ingest([_row(pageviews=20, visits=10)])
        before = RumDaily.objects.get()
        self.ingest([_row(pageviews=20, visits=10)])
        self.assertEqual(RumDaily.objects.get().pageviews, 20)

        result, _statements = self.ingest([_row(pageviews=1), _row(pageviews=31, visits=12)])

        after = RumDaily.objects.get()
        self.assertEqual(result["recorded"]["rows"], 2)
        self.assertEqual(after.pk, before.pk)
        self.assertEqual((after.pageviews, after.visits, after.sample_interval), (31, 12, 10))
        self.assertGreater(after.observed_at, before.observed_at)

    def test_a_restated_day_of_vitals_takes_every_reading(self):
        self.ingest([], [{"date": _day(0), **A_DAY_OF_VITALS, "interaction_to_next_paint_ms": None}])
        before = VitalsDaily.objects.get()
        # A percentile with no samples stays absent rather than becoming a number.
        self.assertIsNone(before.interaction_to_next_paint_ms)

        self.ingest([], [{"date": _day(0), **A_DAY_OF_VITALS, "largest_contentful_paint_ms": None, "lcp_good": 9}])

        after = VitalsDaily.objects.get()
        self.assertEqual(after.pk, before.pk)
        self.assertIsNone(after.largest_contentful_paint_ms)
        self.assertEqual(after.interaction_to_next_paint_ms, 120)
        self.assertEqual((after.lcp_good, after.inp_good), (9, 1))
        self.assertEqual(str(after.cumulative_layout_shift), "0.0500")
        self.assertGreater(after.observed_at, before.observed_at)

    def test_inserting_and_restating_both_move_the_tables_revision(self):
        from hq.platform.core import revisions

        table = RumDaily._meta.db_table
        start = revisions.read().counts.get(table, 0)
        self.ingest([_row(value="/a/"), _row(value="/b/")])
        inserted = revisions.read().counts[table]
        self.ingest([_row(value="/a/"), _row(value="/b/")])
        restated = revisions.read().counts[table]

        self.assertEqual((inserted - start, restated - inserted), (2, 2))

    def test_nothing_about_a_reading_is_audited(self):
        from hq.platform.core.audit import audited_labels

        self.assertFalse({AnalyticsSite, AnalyticsCoverage, RumDaily, VitalsDaily} & set(audited_labels()))


class RecordingTests(TestCase):
    def test_it_stores_a_reading_and_the_site_behind_it_per_connection(self):
        """One site tag read through two connections is two sites."""

        payload = {
            "sites": [
                _site_payload(host="one.example", connection_ref="account-one", rows=[_row()]),
                _site_payload(host="two.example", connection_ref="account-two", rows=[_row()]),
            ]
        }

        service.record_analytics(payload, principal=_principal())

        self.assertEqual(
            set(AnalyticsSite.objects.values_list("site_tag", "connection_ref", "host")),
            {(SITE_TAG, "account-one", "one.example"), (SITE_TAG, "account-two", "two.example")},
        )
        self.assertEqual(RumDaily.objects.count(), 2)
        self.assertEqual(AnalyticsCoverage.objects.count(), 2)

    def test_a_successful_zero_is_coverage_even_without_a_traffic_row(self):
        service.record_analytics(_payload(), principal=_principal())

        self.assertEqual(RumDaily.objects.count(), 0)
        self.assertEqual(
            AnalyticsCoverage.objects.get().date.isoformat(),
            _day(0),
        )

    def test_a_day_outside_the_window_is_not_erased(self):
        """Traffic accumulates; a day outside the window is kept.

        A connection HQ can no longer reach should stop being listed. A day of
        traffic that already happened should not stop having happened because
        the window moved past it.
        """

        service.record_analytics(_payload([_row(offset=90, value="/old/")]), principal=_principal())
        service.record_analytics(_payload([_row(offset=1, value="/new/")]), principal=_principal())

        self.assertEqual(
            sorted(RumDaily.objects.values_list("value", flat=True)),
            ["/new/", "/old/"],
        )

    def test_a_row_with_no_value_is_dropped(self):
        service.record_analytics(
            _payload([_row(value="  "), _row(value="/kept/")]),
            principal=_principal(),
        )

        self.assertEqual(list(RumDaily.objects.values_list("value", flat=True)), ["/kept/"])

    def test_the_contract_allows_exactly_the_dimensions_stored(self):
        from hq.domains.control_plane.bridge_contract import contract

        row = contract()["components"]["schemas"]["AnalyticsRow"]["properties"]
        self.assertEqual(row["dimension"]["enum"], RumDaily.Dimension.values)
        self.assertEqual(row["value"]["maxLength"], RumDaily._meta.get_field("value").max_length)

    def test_recording_requires_the_controller_capability(self):
        bare = Principal(actor="anon", interface="web", capabilities=frozenset())

        with self.assertRaises(AuthorizationError):
            service.record_analytics(_payload([_row()]), principal=bare)

        self.assertEqual(RumDaily.objects.count(), 0)


class CoveragePlanningTests(TestCase):
    def identity(self):
        return [{"connection_ref": "example-api", "site_tag": SITE_TAG}]

    def record_window(self, start_offset: int, end_offset: int = 0):
        payload = _payload()
        payload["sites"][0]["start"] = _day(start_offset)
        payload["sites"][0]["end"] = _day(end_offset)
        service.record_analytics(payload, principal=_principal())

    def test_a_new_site_is_offered_one_bounded_quarter_in_one_query_for_any_number(self):
        identities = [{"connection_ref": "example-api", "site_tag": f"site-{index}"} for index in range(30)]

        with self.assertNumQueries(1):
            windows = service.analytics_plan(identities)["windows"]

        self.assertEqual(len(windows), len(identities))
        for plan in windows:
            self.assertEqual(plan["reason"], "backfill")
            self.assertEqual(
                service.date.fromisoformat(plan["end"]) - service.date.fromisoformat(plan["start"]),
                timedelta(days=service.BACKFILL_DAYS - 1),
            )

    def test_a_complete_site_only_refreshes_recent_completed_days(self):
        self.record_window(service.BACKFILL_DAYS - 1)

        plan = service.analytics_plan(self.identity())["windows"][0]

        self.assertEqual(plan["reason"], "refresh")
        self.assertEqual(
            service.date.fromisoformat(plan["end"]) - service.date.fromisoformat(plan["start"]),
            timedelta(days=service.REFRESH_DAYS - 1),
        )

    def test_a_hole_reopens_only_the_span_needed_to_fill_it(self):
        self.record_window(service.BACKFILL_DAYS - 1)
        AnalyticsCoverage.objects.filter(date=service.date.fromisoformat(_day(20))).delete()

        plan = service.analytics_plan(self.identity())["windows"][0]

        self.assertEqual(plan["reason"], "backfill")
        self.assertEqual(plan["start"], _day(20))
        self.assertEqual(plan["end"], _day(0))

    def test_the_reader_and_provider_mean_the_same_completed_day(self):
        self.assertEqual(
            service._window(1),
            (
                service.date.fromisoformat(_day(0)),
                service.date.fromisoformat(_day(0)),
            ),
        )


class VitalsTests(TestCase):
    def _record(self, **overrides):
        reading = {
            "date": _day(0),
            "sample_interval": 10,
            "largest_contentful_paint_ms": 2888,
            "interaction_to_next_paint_ms": None,
            "first_contentful_paint_ms": 1200,
            "time_to_first_byte_ms": 1721,
            "cumulative_layout_shift": "0.0000",
            "lcp_good": 0,
            "lcp_needs_improvement": 100,
            "lcp_poor": 0,
            "inp_good": 0,
            "inp_needs_improvement": 0,
            "inp_poor": 0,
            "cls_good": 100,
            "cls_needs_improvement": 0,
            "cls_poor": 0,
        } | overrides
        service.record_analytics(_payload(vitals=[reading]), principal=_principal())

    def test_buckets_are_summed_across_days_and_no_samples_is_no_rate(self):
        """A quiet day must not weigh the same as a busy one, and a metric
        nobody sampled has no rate rather than a rate of zero."""

        self._record(date=_day(0), lcp_good=0, lcp_needs_improvement=2, lcp_poor=0)
        self._record(date=_day(1), lcp_good=98, lcp_needs_improvement=0, lcp_poor=0)

        summary = service.vitals_summary(days=7)
        # Averaging the two daily rates would give 0.5.
        self.assertEqual(summary["lcp"]["rate"], 0.98)
        self.assertIsNone(summary["inp"]["rate"])
        self.assertEqual(summary["cls"]["rate"], 1.0)


class JoinTests(TestCase):
    def setUp(self):
        self.writeup = ContentItem.objects.create(
            title="A Writeup",
            slug="a-writeup",
            content_type=ContentItem.Type.PORTFOLIO_PAGE,
            status=ContentItem.Status.PUBLISHED,
            published_url=f"https://{HOST}/portfolio/a-writeup/",
        )
        self.page = ContentItem.objects.create(
            title="Contact",
            slug="contact",
            content_type=ContentItem.Type.PAGE,
            status=ContentItem.Status.PUBLISHED,
            published_url=f"https://{HOST}/contact/",
        )

    def test_a_published_url_joins_to_the_path_the_browser_asked_for(self):
        service.record_analytics(
            _payload([_row(value="/portfolio/a-writeup/", pageviews=412)]),
            principal=_principal(),
        )

        rows = service.writeup_traffic()
        self.assertEqual(rows[0]["item"], self.writeup)
        self.assertEqual(rows[0]["pageviews"], 412)
        self.assertTrue(rows[0]["measured"])

    def test_the_same_path_on_another_host_is_not_credited(self):
        path = "/portfolio/a-writeup/"
        service.record_analytics(
            {
                "sites": [
                    _site_payload(rows=[_row(value=path, pageviews=412)]),
                    _site_payload(
                        site_tag="1" * 32,
                        host="other.example",
                        connection_ref="other-api",
                        rows=[_row(value=path, pageviews=999)],
                    ),
                ]
            },
            principal=_principal(),
        )

        self.assertEqual(service.writeup_traffic()[0]["pageviews"], 412)
        self.assertEqual(service.item_traffic(self.writeup)["pageviews"], 412)

    def test_each_half_sees_its_own_and_keeps_the_unread_last(self):
        """The most actionable row on the page is the one with no number."""

        unread = ContentItem.objects.create(
            title="Unread",
            slug="unread",
            content_type=ContentItem.Type.PORTFOLIO_PAGE,
            status=ContentItem.Status.PUBLISHED,
            published_url=f"https://{HOST}/portfolio/unread/",
        )
        service.record_analytics(
            _payload([_row(value="/portfolio/a-writeup/", pageviews=412), _row(value="/contact/", pageviews=96)]),
            principal=_principal(),
        )

        writeups = service.writeup_traffic()
        self.assertEqual([row["item"] for row in writeups], [self.writeup, unread])
        self.assertEqual([(row["pageviews"], row["measured"]) for row in writeups], [(412, True), (0, False)])
        self.assertEqual([row["item"] for row in service.page_traffic()], [self.page])

    def test_a_trailing_slash_is_not_tidied_away(self):
        """The site serves one form and redirects the other."""

        self.assertEqual(
            service.path_of("https://example.test/portfolio/a-writeup/"),
            "/portfolio/a-writeup/",
        )
        self.assertEqual(service.path_of("https://example.test"), "/")
        self.assertEqual(service.path_of(""), "")


class WindowTests(TestCase):
    def test_a_breakdown_sums_a_window_rather_than_reporting_each_day(self):
        service.record_analytics(
            _payload(
                [
                    _row(value="/about/", offset=0, pageviews=10),
                    _row(value="/about/", offset=1, pageviews=30),
                ]
            ),
            principal=_principal(),
        )

        rows = service.breakdown(RumDaily.Dimension.PATH, days=7)
        self.assertEqual(rows, [{"value": "/about/", "pageviews": 40, "visits": 20}])

    def test_totals_sum_the_window_and_carry_the_least_precise_sampling(self):
        precise = _row(pageviews=20) | {"sample_interval": 1}
        sampled = _row(pageviews=30) | {"sample_interval": 100}
        service.record_analytics(
            {
                "sites": [
                    _site_payload(rows=[precise, _row(offset=400, pageviews=999)]),
                    _site_payload(
                        site_tag="1" * 32,
                        host="other.example",
                        connection_ref="other-api",
                        rows=[sampled],
                    ),
                ]
            },
            principal=_principal(),
        )

        totals = service.site_totals()
        self.assertEqual(totals["pageviews"], 50)
        self.assertEqual(totals["sample_interval"], 100)

    def test_measured_paths_are_counted_at_the_site_path_grain(self):
        service.record_analytics(
            {
                "sites": [
                    _site_payload(rows=[_row(value="/shared/")]),
                    _site_payload(
                        site_tag="1" * 32,
                        host="other.example",
                        connection_ref="other-api",
                        rows=[_row(value="/shared/")],
                    ),
                ]
            },
            principal=_principal(),
        )

        self.assertEqual(service.measured_path_count(), 2)

    def test_nothing_recorded_reads_as_nothing_rather_than_failing(self):
        self.assertIsNone(service.latest_reading())
        self.assertEqual(service.site_totals()["pageviews"], 0)
        self.assertEqual(service.breakdown(RumDaily.Dimension.PATH), [])


class OverviewPageTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="operator", password="not-a-real-password")
        self.client.force_login(user)

    def test_it_says_so_plainly_before_the_first_sweep(self):
        response = self.client.get("/analytics/")

        self.assertIsNone(response.context["latest"])
        self.assertContains(response, "No visits recorded yet.")

    def test_the_page_shows_every_breakdown_its_sampling_and_its_coverage(self):
        service.record_analytics(
            _payload(
                [_row(dimension=dimension, value=f"value-{dimension}") for dimension in RumDaily.Dimension.values]
            ),
            principal=_principal(),
        )

        response = self.client.get("/analytics/?days=7")

        shown = text(response)
        for dimension in RumDaily.Dimension.values:
            self.assertIn(f"value-{dimension}", shown)
        self.assertEqual(response.context["totals"]["sample_interval"], 10)
        coverage = response.context["coverage"]
        self.assertEqual((coverage["covered_days"], coverage["expected_days"], coverage["complete"]), (1, 7, False))
        self.assertFalse(response.context["vitals_measured"])

    def test_a_window_it_does_not_offer_falls_back_to_the_default(self):
        service.record_analytics(_payload([_row()]), principal=_principal())

        for asked in ("banana", "99999"):
            with self.subTest(asked=asked):
                response = self.client.get(f"/analytics/?days={asked}")

                self.assertEqual(response.context["days"], service.DEFAULT_WINDOW_DAYS)

    def test_signing_in_is_required(self):
        self.client.logout()

        response = self.client.get("/analytics/")

        self.assertEqual(response.status_code, 302)


class ContentSectionTrafficTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="operator", password="not-a-real-password")
        self.client.force_login(user)
        self.read = ContentItem.objects.create(
            title="Read",
            slug="read",
            content_type=ContentItem.Type.PORTFOLIO_PAGE,
            status=ContentItem.Status.PUBLISHED,
            published_url=f"https://{HOST}/portfolio/read/",
        )
        self.unread = ContentItem.objects.create(
            title="Unread",
            slug="unread",
            content_type=ContentItem.Type.PORTFOLIO_PAGE,
            status=ContentItem.Status.PUBLISHED,
            published_url=f"https://{HOST}/portfolio/unread/",
        )
        service.record_analytics(
            _payload([_row(value="/portfolio/read/", pageviews=412)]),
            principal=_principal(),
        )

    def test_unmeasured_is_absent_rather_than_zero(self):
        """Nobody visited and nobody looked are different claims."""

        attached = {item.slug: item.pageviews for item in service.attach_traffic([self.read, self.unread])}

        self.assertEqual(attached["read"], 412)
        self.assertIsNone(attached["unread"])

    def test_each_section_lists_its_own_half_with_what_each_earned(self):
        page = ContentItem.objects.create(
            title="Contact",
            slug="contact",
            content_type=ContentItem.Type.PAGE,
            status=ContentItem.Status.PUBLISHED,
            published_url=f"https://{HOST}/contact/",
        )

        writeups = self.client.get("/content/writeups/")
        pages = self.client.get("/content/pages/")

        self.assertIn("412", text(writeups))
        listed = {"writeups": set(hrefs(writeups)), "pages": set(hrefs(pages))}
        self.assertLessEqual({self.read.get_absolute_url(), self.unread.get_absolute_url()}, listed["writeups"])
        self.assertNotIn(page.get_absolute_url(), listed["writeups"])
        self.assertIn(page.get_absolute_url(), listed["pages"])
        self.assertNotIn(self.unread.get_absolute_url(), listed["pages"])

    def test_the_join_costs_one_query_however_many_rows(self):
        """The projection grows with content; its query count must not."""

        for index in range(20):
            ContentItem.objects.create(
                title=f"Extra {index}",
                slug=f"extra-{index}",
                content_type=ContentItem.Type.PORTFOLIO_PAGE,
                status=ContentItem.Status.PUBLISHED,
                published_url=f"https://{HOST}/portfolio/extra-{index}/",
            )

        # Materialised outside the block: the claim is about the join, not
        # about the queryset the table engine already paid for.
        items = list(ContentItem.objects.all())

        with self.assertNumQueries(1):
            service.attach_traffic(items)

    def test_nothing_measured_leaves_the_section_readable(self):
        RumDaily.objects.all().delete()

        response = self.client.get("/content/writeups/")

        self.assertIn(self.read.get_absolute_url(), hrefs(response))


class RegisteredResourceTests(TestCase):
    """Analytics has to be reachable the same way every other domain is."""

    def setUp(self):
        service.record_analytics(
            _payload(
                [
                    _row(value="/about/", pageviews=40),
                    _row(dimension="country", value="Germany", pageviews=25),
                ]
            ),
            principal=_principal(),
        )

    def test_it_is_listable_through_the_shared_resource_contract_by_breakdown(self):
        for arguments, dimension, values in (
            ({}, "path", ["/about/"]),
            ({"dimension": "country"}, "country", ["Germany"]),
        ):
            with self.subTest(dimension=dimension):
                result = list_resource("analytics", arguments, principal=_principal())

                self.assertEqual(result["count"], len(result["items"]))
                self.assertEqual([row["value"] for row in result["items"]], values)
                self.assertEqual({row["dimension"] for row in result["items"]}, {dimension})

    def test_a_dimension_nobody_records_is_refused(self):
        with self.assertRaises(ValueError):
            service.list_analytics(dimension="fingerprint")

    def test_an_unknown_filter_is_rejected_before_the_handler(self):
        with self.assertRaises(InvalidResourceInput):
            list_resource("analytics", {"nope": 1}, principal=_principal())

    def test_a_window_beyond_retention_is_refused_rather_than_answered_emptily(self):
        with self.assertRaises(InvalidResourceInput):
            list_resource("analytics", {"days": 500}, principal=_principal())

    def test_reading_requires_a_capability(self):
        bare = Principal(actor="anon", interface="web", capabilities=frozenset())

        with self.assertRaises(AuthorizationError):
            list_resource("analytics", {}, principal=bare)

    def test_it_carries_the_sampling_into_the_machine_surface(self):
        result = service.list_analytics()

        self.assertEqual(result["sample_interval"], 10)


class RecordedDayTests(TestCase):
    def test_the_latest_reading_is_the_newest_day_any_site_reported(self):
        service.record_analytics(
            _payload([_row(offset=3), _row(offset=0, value="/newer/")]),
            principal=_principal(),
        )

        self.assertEqual(service.latest_reading(), timezone.now().date() - timedelta(days=1))
