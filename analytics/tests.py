"""What a reading has to survive between Cloudflare and a page.

The shapes here are the ones the live API actually returns -- verified against
it -- with synthetic values, because this repository is public and a real site
tag and its traffic are not things to publish alongside the code that reads
them.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from analytics.models import AnalyticsSite, RumDaily, VitalsDaily
from application import analytics as service
from application.security import AuthorizationError, Capability, Principal
from content.models import ContentItem

SITE_TAG = "0" * 32
HOST = "example.test"


def _principal() -> Principal:
    return Principal(
        actor="controller",
        interface="cli",
        capabilities=frozenset({Capability.MANAGE_INFRASTRUCTURE, Capability.READ}),
    )


def _day(offset: int) -> str:
    return (timezone.now().date() - timedelta(days=offset)).isoformat()


def _payload(rows=None, vitals=None) -> dict:
    return {
        "sites": [
            {
                "site_tag": SITE_TAG,
                "host": HOST,
                "connection_ref": "example-api",
                "rows": rows if rows is not None else [],
                "vitals": vitals if vitals is not None else [],
            }
        ]
    }


def _site_payload(
    *, site_tag=SITE_TAG, host=HOST, connection_ref="example-api", rows=None
) -> dict:
    return {
        "site_tag": site_tag,
        "host": host,
        "connection_ref": connection_ref,
        "rows": rows if rows is not None else [],
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


class RecordingTests(TestCase):
    def test_it_stores_a_reading_and_the_site_behind_it(self):
        service.record_analytics(_payload([_row()]), principal=_principal())

        site = AnalyticsSite.objects.get(site_tag=SITE_TAG)
        self.assertEqual(site.host, HOST)
        self.assertEqual(site.connection_ref, "example-api")
        self.assertEqual(RumDaily.objects.count(), 1)

    def test_replaying_a_sweep_restates_a_day_rather_than_doubling_it(self):
        payload = _payload([_row(pageviews=20)])
        service.record_analytics(payload, principal=_principal())
        service.record_analytics(payload, principal=_principal())

        self.assertEqual(RumDaily.objects.count(), 1)
        self.assertEqual(RumDaily.objects.get().pageviews, 20)

    def test_a_later_sweep_corrects_a_day_it_reports_again(self):
        service.record_analytics(_payload([_row(pageviews=20)]), principal=_principal())
        service.record_analytics(_payload([_row(pageviews=50)]), principal=_principal())

        self.assertEqual(RumDaily.objects.get().pageviews, 50)

    def test_a_day_outside_the_window_is_not_erased(self):
        """The one way this differs from every other controller report.

        A connection HQ can no longer reach should stop being listed. A day of
        traffic that already happened should not stop having happened because
        the window moved past it.
        """

        service.record_analytics(
            _payload([_row(offset=90, value="/old/")]), principal=_principal()
        )
        service.record_analytics(
            _payload([_row(offset=1, value="/new/")]), principal=_principal()
        )

        self.assertEqual(
            sorted(RumDaily.objects.values_list("value", flat=True)),
            ["/new/", "/old/"],
        )

    def test_a_row_with_no_value_is_dropped(self):
        service.record_analytics(
            _payload([_row(value="  "), _row(value="/kept/")]),
            principal=_principal(),
        )

        self.assertEqual(
            list(RumDaily.objects.values_list("value", flat=True)), ["/kept/"]
        )

    def test_an_unknown_dimension_is_refused_rather_than_stored(self):
        service.record_analytics(
            _payload([_row(dimension="fingerprint", value="x")]),
            principal=_principal(),
        )

        self.assertEqual(RumDaily.objects.count(), 0)

    def test_recording_requires_the_controller_capability(self):
        bare = Principal(actor="anon", interface="web", capabilities=frozenset())

        with self.assertRaises(AuthorizationError):
            service.record_analytics(_payload([_row()]), principal=bare)

        self.assertEqual(RumDaily.objects.count(), 0)

    def test_the_same_site_tag_can_exist_in_independent_connections(self):
        payload = {
            "sites": [
                _site_payload(
                    host="one.example", connection_ref="account-one", rows=[_row()]
                ),
                _site_payload(
                    host="two.example", connection_ref="account-two", rows=[_row()]
                ),
            ]
        }

        service.record_analytics(payload, principal=_principal())

        self.assertEqual(AnalyticsSite.objects.count(), 2)
        self.assertEqual(
            set(AnalyticsSite.objects.values_list("connection_ref", "host")),
            {("account-one", "one.example"), ("account-two", "two.example")},
        )
        self.assertEqual(RumDaily.objects.count(), 2)


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

    def test_a_percentile_with_no_samples_stays_absent(self):
        """``-1`` is absence wearing a number's clothes; it must not become one."""

        self._record()

        stored = VitalsDaily.objects.get()
        self.assertIsNone(stored.interaction_to_next_paint_ms)
        self.assertEqual(stored.largest_contentful_paint_ms, 2888)

    def test_a_metric_with_no_samples_reports_no_rate_rather_than_zero(self):
        self._record()

        summary = service.vitals_summary(days=7)
        self.assertIsNone(summary["inp"]["rate"])
        self.assertEqual(summary["lcp"]["rate"], 0.0)
        self.assertEqual(summary["cls"]["rate"], 1.0)

    def test_buckets_are_summed_across_days_before_dividing(self):
        """A quiet day must not weigh the same as a busy one."""

        self._record(date=_day(0), lcp_good=0, lcp_needs_improvement=2, lcp_poor=0)
        self._record(date=_day(1), lcp_good=98, lcp_needs_improvement=0, lcp_poor=0)

        # Averaging the two daily rates would give 0.5. Summing first gives the
        # rate an operator would compute by hand from the underlying samples.
        self.assertEqual(service.vitals_summary(days=7)["lcp"]["rate"], 0.98)


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

    def test_the_two_halves_do_not_see_each_other(self):
        service.record_analytics(
            _payload(
                [
                    _row(value="/portfolio/a-writeup/", pageviews=412),
                    _row(value="/contact/", pageviews=96),
                ]
            ),
            principal=_principal(),
        )

        self.assertEqual(
            [row["item"] for row in service.writeup_traffic()], [self.writeup]
        )
        self.assertEqual([row["item"] for row in service.page_traffic()], [self.page])

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

    def test_something_published_and_unread_is_kept_and_sorted_last(self):
        """The most actionable row on the page is the one with no number."""

        service.record_analytics(
            _payload([_row(value="/contact/", pageviews=96)]), principal=_principal()
        )

        rows = service.page_traffic()
        self.assertEqual([row["item"].slug for row in rows], ["contact"])
        writeups = service.writeup_traffic()
        self.assertEqual(writeups[0]["pageviews"], 0)
        self.assertFalse(writeups[0]["measured"])

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

    def test_a_day_older_than_the_window_is_left_out_of_the_reading(self):
        service.record_analytics(
            _payload([_row(offset=400, pageviews=999)]), principal=_principal()
        )

        self.assertEqual(service.site_totals()["pageviews"], 0)

    def test_totals_carry_the_sampling_the_figure_rests_on(self):
        service.record_analytics(_payload([_row()]), principal=_principal())

        self.assertEqual(service.site_totals()["sample_interval"], 10)

    def test_totals_carry_the_least_precise_sampling_across_sites(self):
        precise = _row(pageviews=20) | {"sample_interval": 1}
        sampled = _row(pageviews=30) | {"sample_interval": 100}
        service.record_analytics(
            {
                "sites": [
                    _site_payload(rows=[precise]),
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

    def test_every_dimension_the_reader_collects_can_be_stored(self):
        """The reader's registry and the model's enum have to stay in step."""

        from controller_runtime.providers import ANALYTICS_DIMENSIONS

        self.assertEqual(
            sorted(ANALYTICS_DIMENSIONS), sorted(RumDaily.Dimension.values)
        )

    def test_nothing_recorded_reads_as_nothing_rather_than_failing(self):
        self.assertIsNone(service.latest_reading())
        self.assertEqual(service.site_totals()["pageviews"], 0)
        self.assertEqual(service.breakdown(RumDaily.Dimension.PATH), [])


class UnitTests(TestCase):
    def test_microseconds_become_milliseconds_and_minus_one_becomes_absence(self):
        from controller_runtime.providers import _milliseconds

        self.assertEqual(_milliseconds(2888000), 2888)
        self.assertEqual(_milliseconds(0), 0)
        self.assertIsNone(_milliseconds(-1))
        self.assertIsNone(_milliseconds(None))
        self.assertIsNone(_milliseconds("not a number"))


class OverviewPageTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(user)

    def test_it_says_so_plainly_before_the_first_sweep(self):
        response = self.client.get("/analytics/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No readings yet")

    def test_every_breakdown_the_reader_collects_reaches_the_page(self):
        service.record_analytics(
            _payload(
                [
                    _row(dimension=dimension, value=f"value-{dimension}")
                    for dimension in RumDaily.Dimension.values
                ]
            ),
            principal=_principal(),
        )

        response = self.client.get("/analytics/")

        for dimension in RumDaily.Dimension.values:
            self.assertContains(response, f"value-{dimension}")

    def test_an_unreadable_window_shows_traffic_rather_than_an_error(self):
        service.record_analytics(_payload([_row()]), principal=_principal())

        response = self.client.get("/analytics/?days=banana")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["days"], service.DEFAULT_WINDOW_DAYS)

    def test_a_window_nobody_offers_falls_back_rather_than_being_honoured(self):
        response = self.client.get("/analytics/?days=99999")

        self.assertEqual(response.context["days"], service.DEFAULT_WINDOW_DAYS)

    def test_it_says_when_a_figure_is_an_estimate(self):
        service.record_analytics(_payload([_row()]), principal=_principal())

        response = self.client.get("/analytics/")

        self.assertContains(response, "sampled 1:10")

    def test_signing_in_is_required(self):
        self.client.logout()

        response = self.client.get("/analytics/")

        self.assertEqual(response.status_code, 302)


class ContentSectionTrafficTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
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

        attached = {
            item.slug: item.pageviews
            for item in service.attach_traffic([self.read, self.unread])
        }

        self.assertEqual(attached["read"], 412)
        self.assertIsNone(attached["unread"])

    def test_the_writeups_section_shows_what_each_earned(self):
        response = self.client.get("/content/writeups/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "412")
        self.assertContains(response, "Views")

    def test_the_two_sections_show_their_own_half(self):
        ContentItem.objects.create(
            title="Contact",
            slug="contact",
            content_type=ContentItem.Type.PAGE,
            status=ContentItem.Status.PUBLISHED,
            published_url=f"https://{HOST}/contact/",
        )

        writeups = self.client.get("/content/writeups/")
        pages = self.client.get("/content/pages/")

        self.assertContains(writeups, "Unread")
        self.assertNotContains(writeups, "Contact</strong>")
        self.assertContains(pages, "Contact")
        self.assertNotContains(pages, "Unread</strong>")

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

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Read")


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

    def test_it_is_listable_through_the_shared_resource_contract(self):
        from application.resources import list_resource

        result = list_resource("analytics", {}, principal=_principal())

        self.assertEqual(result["count"], len(result["items"]))
        self.assertEqual(result["items"][0]["value"], "/about/")
        self.assertEqual(result["items"][0]["dimension"], "path")

    def test_a_breakdown_can_be_asked_for_by_name(self):
        from application.resources import list_resource

        result = list_resource(
            "analytics", {"dimension": "country"}, principal=_principal()
        )

        self.assertEqual([row["value"] for row in result["items"]], ["Germany"])

    def test_a_dimension_nobody_records_is_refused(self):
        with self.assertRaises(ValueError):
            service.list_analytics(dimension="fingerprint")

    def test_an_unknown_filter_is_rejected_before_the_handler(self):
        from application.resources import InvalidResourceInput, list_resource

        with self.assertRaises(InvalidResourceInput):
            list_resource("analytics", {"nope": 1}, principal=_principal())

    def test_a_window_beyond_retention_is_refused_rather_than_answered_emptily(self):
        from application.resources import InvalidResourceInput, list_resource

        with self.assertRaises(InvalidResourceInput):
            list_resource("analytics", {"days": 500}, principal=_principal())

    def test_reading_requires_a_capability(self):
        from application.resources import list_resource
        from application.security import AuthorizationError

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

        self.assertEqual(service.latest_reading(), timezone.now().date())
