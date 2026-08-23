from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.template.loader import render_to_string
from django.utils import timezone

from control_plane.models import ManagedResource, OperationRequest
from expenses.models import Expense
from projects.models import Project

from .dashboard import operating_snapshot
from .infrastructure import get_managed_resource, operation_summary
from .read_models import change_feed
from .ui import (
    PLOT_LEFT,
    PLOT_WIDTH,
    ChartSeries,
    PageNavigation,
    PageSection,
    Timeline,
    TimelineItem,
    line_chart,
    stacked_bar_chart,
)


class UiProjectionTests(TestCase):
    def test_page_navigation_is_a_stable_accessible_fragment_map(self):
        navigation = PageNavigation(
            (PageSection("overview", "Overview"), PageSection("recent-work", "Recent work"))
        )

        rendered = render_to_string(
            "partials/_page_navigation.html", {"navigation": navigation}
        )

        self.assertIn('aria-label="On this page"', rendered)
        self.assertIn('href="#recent-work"', rendered)

    def test_page_navigation_rejects_ambiguous_or_unsafe_destinations(self):
        with self.assertRaisesMessage(ValueError, "only lowercase"):
            PageSection("Recent work", "Recent work")
        with self.assertRaisesMessage(ValueError, "must be unique"):
            PageNavigation(
                (PageSection("overview", "Overview"), PageSection("overview", "Again"))
            )

    def test_timeline_requires_chronological_items_and_renders_links(self):
        item = TimelineItem(
            date(2026, 11, 1),
            "Enrollment opens",
            "Available on this date",
            "/tasks/enroll/",
            "good",
            "opens",
        )
        timeline = Timeline("Next", "The planning horizon.", (item,))

        rendered = render_to_string(
            "partials/_timeline.html", {"timeline": timeline}
        )

        self.assertIn('datetime="2026-11-01"', rendered)
        self.assertIn('href="/tasks/enroll/"', rendered)

    def test_timeline_rejects_unsorted_events(self):
        with self.assertRaisesMessage(ValueError, "chronologically"):
            Timeline(
                "Next",
                "",
                (
                    TimelineItem(date(2027, 1, 1), "Later"),
                    TimelineItem(date(2026, 1, 1), "Sooner"),
                ),
            )

    def test_stacked_chart_projects_aligned_series_once(self):
        chart = stacked_bar_chart(
            "Training",
            "Weekly minutes",
            ("Aug 3", "Aug 10"),
            (
                ChartSeries("Run", (30.0, 45.0), 1),
                ChartSeries("Strength", (60.0, 30.0), 2),
            ),
            unit="minutes",
        )

        self.assertFalse(chart.empty)
        self.assertEqual(chart.rows[1].values, (45.0, 30.0))
        self.assertEqual(len(chart.bars), 4)
        rendered = render_to_string(
            "partials/_stacked_bar_chart.html", {"chart": chart}
        )
        self.assertIn("Training", rendered)
        self.assertIn("View chart data", rendered)
        # The tooltip names its period as well as its series: hovering a bar
        # without being told which week it is answers half a question.
        self.assertIn('data-tip="Aug 10 · Run: 45 minutes"', rendered)
        # No SVG <title>: that is the browser's own tooltip, which waits a
        # second or two and would double up with the host's.
        self.assertNotIn("<title>", rendered)

    def test_stacked_chart_rejects_misaligned_series(self):
        with self.assertRaises(ValueError):
            stacked_bar_chart(
                "Training",
                "Weekly minutes",
                ("Aug 3",),
                (ChartSeries("Run", (30.0, 45.0), 1),),
                unit="minutes",
            )


class LineChartTests(TestCase):
    """A measure over time, on an axis fitted to the measure."""

    SERIES = (
        (
            "Resting heart rate",
            (
                (date(2026, 1, 1), 52.0),
                (date(2026, 1, 15), 54.0),
                (date(2026, 2, 1), 51.0),
            ),
            1,
        ),
    )

    def test_the_axis_is_fitted_to_the_data_not_to_zero(self):
        chart = line_chart("Resting", "", self.SERIES, unit="bpm")
        floor = float(chart.ticks[0].label.replace(",", ""))
        # A zero-based axis draws 51, 52 and 54 as three identical heights and
        # the chart then says nothing happened. That is the whole reason this
        # primitive exists beside the bar chart.
        self.assertGreater(floor, 45.0)
        self.assertLess(floor, 52.0)

    def test_dates_are_placed_by_the_calendar_not_by_index(self):
        chart = line_chart("Resting", "", self.SERIES, unit="bpm")
        points = chart.series[0].points
        first, middle, last = (p.x for p in points)
        # Jan 1 → Jan 15 is a fortnight and Jan 15 → Feb 1 is longer, so the
        # second gap has to be the wider one. Plotted against index they would
        # be equal, which would restate the calendar.
        self.assertLess(middle - first, last - middle)

    def test_a_narrow_range_keeps_the_ticks_distinguishable(self):
        # Pace across a run: 10.6 to 11.8 min/mi. Rounded to whole numbers the
        # three ticks read 11, 11, 12, which looks like a bug and carries no
        # information about the axis it labels.
        chart = line_chart(
            "Pace",
            "",
            (
                (
                    "Pace",
                    (
                        (date(2026, 1, 1), 10.6),
                        (date(2026, 1, 2), 11.2),
                        (date(2026, 1, 3), 11.8),
                    ),
                    1,
                ),
            ),
            unit="min/mi",
        )
        labels = [tick.label for tick in chart.ticks]
        self.assertEqual(len(set(labels)), len(labels))
        self.assertTrue(all("." in label for label in labels), labels)

    def test_a_wide_range_still_drops_the_decimals(self):
        chart = line_chart(
            "Steps",
            "",
            (
                (
                    "Steps",
                    ((date(2026, 1, 1), 2000.0), (date(2026, 1, 2), 18000.0)),
                    1,
                ),
            ),
            unit="steps",
        )
        self.assertTrue(any("k" in tick.label for tick in chart.ticks))

    def test_the_axis_does_not_pad_below_a_natural_floor(self):
        # Walking asymmetry is a share of steps. Padding 0.1 down by 8% of the
        # range draws an axis reaching below zero, which is a region no reading
        # can occupy.
        chart = line_chart(
            "Asymmetry",
            "",
            (
                (
                    "Asymmetry",
                    (
                        (date(2026, 1, 1), 0.1),
                        (date(2026, 1, 2), 1.4),
                        (date(2026, 1, 3), 3.0),
                    ),
                    1,
                ),
            ),
            unit="%",
        )
        self.assertEqual(float(chart.ticks[0].label), 0.0)

    def test_a_series_that_does_go_negative_still_gets_its_room(self):
        chart = line_chart(
            "Drift",
            "",
            (
                (
                    "Drift",
                    ((date(2026, 1, 1), -4.0), (date(2026, 1, 2), 6.0)),
                    1,
                ),
            ),
            unit="",
        )
        self.assertLess(float(chart.ticks[0].label), -4.0)

    def test_a_flat_series_is_still_drawn_inside_the_plot(self):
        chart = line_chart(
            "Flat",
            "",
            (("Steady", ((date(2026, 1, 1), 7.0), (date(2026, 1, 2), 7.0)), 1),),
            unit="h",
        )
        self.assertFalse(chart.empty)
        for point in chart.series[0].points:
            self.assertGreater(point.y, 12.0)
            self.assertLess(point.y, 214.0)

    def test_one_reading_is_not_a_chart(self):
        chart = line_chart(
            "Lonely", "", (("Only", ((date(2026, 1, 1), 7.0),), 1),), unit="h"
        )
        self.assertTrue(chart.empty)

    def test_a_mark_lands_on_its_own_date(self):
        chart = line_chart(
            "Resting",
            "",
            self.SERIES,
            unit="bpm",
            marks=((date(2026, 1, 15), "Care"),),
        )
        self.assertEqual(len(chart.marks), 1)
        self.assertAlmostEqual(
            chart.marks[0].x, chart.series[0].points[1].x, places=1
        )

    def test_a_mark_outside_the_window_is_dropped(self):
        chart = line_chart(
            "Resting", "", self.SERIES, unit="bpm", marks=((date(2025, 1, 1), "Old"),)
        )
        self.assertEqual(chart.marks, ())

    def test_the_line_is_stroked_rather_than_filled(self):
        chart = line_chart("Resting", "", self.SERIES, unit="bpm", trend=True)
        rendered = render_to_string("partials/_line_chart.html", {"chart": chart})
        # `.chart-series-N` sets `fill`, which would render the path as a
        # filled blob. Lines use the stroke classes.
        self.assertIn('class="chart-line chart-line-1"', rendered)
        self.assertIn("chart-trend", rendered)
        self.assertIn("View chart data", rendered)
        self.assertNotIn("<title>", rendered)

    def test_each_point_has_a_target_big_enough_to_hit(self):
        chart = line_chart("Resting", "", self.SERIES, unit="bpm")
        rendered = render_to_string("partials/_line_chart.html", {"chart": chart})
        # The visible dot is three pixels across, which is smaller than a
        # pointer can be aimed: the tooltip worked and could not be reached.
        # The tip belongs to an invisible circle several times its size.
        self.assertIn('class="chart-hit"', rendered)
        self.assertIn('r="11"', rendered)
        self.assertNotIn('r="3" data-tip', rendered)
        # And the target has to be drawn after the lines, or a line crossing it
        # takes the pointer first.
        self.assertGreater(
            rendered.index("chart-hit"), rendered.rindex("chart-line-")
        )

    def test_every_point_carries_the_hosts_own_tooltip(self):
        chart = line_chart("Resting", "", self.SERIES, unit="bpm")
        rendered = render_to_string("partials/_line_chart.html", {"chart": chart})
        self.assertIn("Jan 1, 2026 · Resting heart rate: 52 bpm", rendered)
        self.assertNotIn("title=", rendered)

    def test_a_slot_outside_the_palette_is_refused(self):
        with self.assertRaises(ValueError):
            line_chart(
                "Resting",
                "",
                (("Resting", ((date(2026, 1, 1), 1.0), (date(2026, 1, 2), 2.0)), 9),),
                unit="bpm",
            )

    def test_the_plot_rectangle_is_shared_with_the_bar_chart(self):
        line = line_chart("Resting", "", self.SERIES, unit="bpm")
        bars = stacked_bar_chart(
            "Training", "", ("a", "b"), (ChartSeries("Run", (1.0, 2.0), 1),), unit="m"
        )
        # Two charts stacked in a column have to share an axis position, or the
        # page reads as two unrelated drawings. A bar is centred in its own
        # column so its x is not the plot's left edge; the axis is what has to
        # line up, and the line does start at the edge.
        self.assertEqual(line.ticks[0].y, bars.ticks[0].y)
        self.assertEqual(line.ticks[-1].y, bars.ticks[-1].y)
        # Against the constants, not against 48 and 702. Written as literals
        # this test pinned a copy of the geometry rather than the geometry, so
        # moving the plot broke the test that exists to prove the plot moved
        # everywhere at once -- the same copied-constant fault the templates
        # had.
        self.assertAlmostEqual(line.series[0].points[0].x, PLOT_LEFT, places=1)
        self.assertAlmostEqual(
            line.series[0].points[-1].x, PLOT_LEFT + PLOT_WIDTH, places=1
        )
        self.assertAlmostEqual(line.plot_right, bars.plot_right, places=1)
        self.assertAlmostEqual(line.plot_left, bars.plot_left, places=1)


class ChartAxisSpanTests(TestCase):
    """What the axis calls a date depends on how much time the chart covers."""

    def _chart(self, days):
        start = date(2020, 1, 15)
        points = tuple(
            (start + timedelta(days=step), 1.0 + step / 100)
            for step in range(0, days, max(1, days // 40))
        )
        return line_chart("Span", "", (("Measure", points, 1),), unit="u")

    def test_a_short_range_labels_the_day(self):
        labels = [item.label for item in self._chart(90).categories]

        self.assertTrue(any("15" in label for label in labels), labels)
        self.assertFalse(any("2020" in label for label in labels), labels)

    def test_a_multi_year_range_labels_the_year(self):
        # Six years of monthly readings on an axis reading "Sep 15" is an axis
        # that looks like one year. A clinician cannot tell 2021 from 2026,
        # which is the entire content of a long-run chart.
        labels = [item.label for item in self._chart(365 * 6).categories]

        self.assertTrue(all(any(ch.isdigit() for ch in label) for label in labels))
        self.assertTrue(any("202" in label for label in labels), labels)


# What the host's own dashboard is allowed to cost, and what each installed
# extension may add on top. Two numbers because the dashboard composes every
# domain: a single fixed budget is wrong by construction the moment an extension
# is installed, and it failed exactly that way in the composed image while
# passing everywhere else.
HOST_QUERY_BUDGET = 34
PER_EXTENSION_QUERY_BUDGET = 10


class DashboardProjectionTests(TestCase):
    @staticmethod
    def _snapshot_queries():
        with (
            patch("application.attention.get_unread_count", return_value=0),
            CaptureQueriesContext(connection) as queries,
        ):
            operating_snapshot()
        return queries

    def test_snapshot_stays_within_its_query_budget(self):
        """HQ's own cost, measured without whatever happens to be installed.

        Extensions are patched out rather than tolerated. The number this
        guards is what the host spends assembling its own page, and letting an
        installed extension move it means the guard reports on the environment
        instead of on the code.
        """
        Project.objects.create(
            name="Query budget",
            slug="query-budget",
            status=Project.Status.ACTIVE,
        )

        # The snapshot assembles the whole page -- KPIs, the composed queue, the
        # card row and the recent lists -- in one call, so this covers all of it
        # rather than a part. Six are a section's reading answered once for its
        # card and once for the KPI block. Six more are the service view, which
        # is a join rather than a table: resources, the topology snapshot and
        # published projects, derived once for the queue and once for the card.
        # On local SQLite that is microseconds, and the duplication is visible
        # here rather than hidden behind a cache that could go stale -- the last
        # cache tried here served a stale count to a test, which then passed
        # while asserting the wrong answer.
        with patch("application.domains.extension_domains", return_value=()):
            queries = self._snapshot_queries()

        # Counts only. This assertion runs in the composed image too, where the
        # captured SQL names the private extensions' tables and columns -- and
        # a failing public CI job prints its message into a world-readable log.
        self.assertLessEqual(
            len(queries),
            HOST_QUERY_BUDGET,
            f"Host dashboard used {len(queries)} queries against a budget of "
            f"{HOST_QUERY_BUDGET}. Run this locally to see them.",
        )

    def test_each_installed_extension_costs_a_bounded_amount(self):
        """The composed page has to stay affordable as domains are added.

        Trivially true with none installed, which is the point: the same
        assertion is meaningful in the composed image and silent in host-only
        CI, instead of a fixed number that is simply wrong in one of them.
        """
        from application.domains import extension_domains

        installed = len(extension_domains())
        allowed = HOST_QUERY_BUDGET + installed * PER_EXTENSION_QUERY_BUDGET

        used = len(self._snapshot_queries())

        self.assertLessEqual(
            used,
            allowed,
            f"Composed dashboard used {used} queries; {installed} extension(s) "
            f"allow {allowed}. Run this locally to see them.",
        )

    def test_snapshot_is_json_safe_and_owns_priority_counts(self):
        Project.objects.create(
            name="Needs output",
            slug="needs-output",
            status=Project.Status.ACTIVE,
        )

        with patch("application.attention.get_unread_count", return_value=2):
            snapshot = operating_snapshot()

        json.dumps(snapshot)
        # Keyed by the domain that raised it. Entries no longer carry a
        # hand-assigned code, because nothing needs one: the link an entry
        # points at travels with the entry.
        items = {item["source_id"]: item for item in snapshot["priority"]}
        self.assertEqual(items["hq.projects"]["count"], 1)
        self.assertEqual(items["hq.contacts"]["count"], 2)
        self.assertTrue(items["hq.projects"]["url"])
        self.assertEqual(
            snapshot["priority_count"],
            sum(item["count"] for item in snapshot["priority"]),
        )

    def test_expense_kpis_respect_fiscal_year_start(self):
        today = timezone.localdate()
        Expense.objects.create(
            date=today,
            vendor="Now",
            item="This fiscal year",
            category="hosting",
            total_cost=Decimal("10.00"),
        )
        Expense.objects.create(
            date=today - timedelta(days=45),
            vendor="Then",
            item="Before the fiscal year started",
            category="hosting",
            total_cost=Decimal("7.00"),
        )
        Expense.objects.create(
            date=today + timedelta(days=1),
            vendor="Future",
            item="Not year-to-date yet",
            category="hosting",
            total_cost=Decimal("99.00"),
        )

        # Fiscal year starting this month: the 45-day-old expense falls outside.
        with (
            override_settings(SEVERINO_FISCAL_YEAR_START_MONTH=today.month),
            patch("application.attention.get_unread_count", return_value=0),
        ):
            snapshot = operating_snapshot()
        self.assertEqual(snapshot["kpis"]["expenses_count"], 1)
        self.assertEqual(Decimal(snapshot["kpis"]["expenses_total"]), Decimal("10.00"))

        # Fiscal year starting next month began ~11 months ago: both past
        # expenses fall inside, while the future expense remains excluded.
        with (
            override_settings(SEVERINO_FISCAL_YEAR_START_MONTH=today.month % 12 + 1),
            patch("application.attention.get_unread_count", return_value=0),
        ):
            snapshot = operating_snapshot()
        self.assertEqual(snapshot["kpis"]["expenses_count"], 2)


class ChangeFeedTests(TestCase):
    """A cursor a phone can hold across launches, sleeps, and network gaps."""

    def test_a_first_sync_gets_the_head_and_no_backlog(self):
        Project.objects.create(slug="alpha", name="Alpha")
        feed = change_feed()
        self.assertEqual(feed["items"], [])
        self.assertFalse(feed["has_more"])
        self.assertGreater(feed["cursor"], 0)

    def test_only_changes_after_the_cursor_come_back(self):
        Project.objects.create(slug="alpha", name="Alpha")
        cursor = change_feed()["cursor"]
        Project.objects.create(slug="beta", name="Beta")

        feed = change_feed(since=cursor)
        self.assertEqual([item["object_id"] for item in feed["items"]], ["2"])
        self.assertEqual(feed["items"][0]["action"], "created")
        self.assertGreater(feed["cursor"], cursor)

        # Replaying the returned cursor is idempotent: no event is delivered
        # twice, which is what makes it safe to persist and resume from.
        self.assertEqual(change_feed(since=feed["cursor"])["items"], [])

    def test_the_feed_carries_no_free_text(self):
        Project.objects.create(slug="alpha", name="Alpha")
        cursor = change_feed()["cursor"]
        Project.objects.create(slug="beta", name="Beta")
        self.assertEqual(
            set(change_feed(since=cursor)["items"][0]),
            {"id", "action", "object_type", "object_id", "created_at"},
        )

    def test_a_partial_page_advertises_more(self):
        cursor = change_feed()["cursor"]
        for index in range(4):
            Project.objects.create(slug=f"p{index}", name=f"P{index}")
        feed = change_feed(since=cursor, limit=2)
        self.assertEqual(feed["count"], 2)
        self.assertTrue(feed["has_more"])
        self.assertFalse(change_feed(since=feed["cursor"], limit=2)["has_more"])


class OperationProjectionTests(TestCase):
    def test_failed_operation_separates_guidance_and_affected_evidence(self):
        resource = ManagedResource.objects.create(
            key="certificate",
            kind="tls.certificate",
            spec={},
        )
        operation = OperationRequest.objects.create(
            resource=resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.FAILED,
            requested_actor="homelab-controller",
            requested_interface="controller",
            idempotency_key="failed-projection",
            result={
                "message": "Verification did not converge.",
                "conditions": [
                    {
                        "type": "Degraded",
                        "status": True,
                        "reason": "VerificationFailed",
                        "message": "One consumer serves the previous certificate.",
                    }
                ],
                "status": {
                    "expected_fingerprint_sha256": "expected",
                    "consumers": [
                        {
                            "consumer": "npm",
                            "domain": "hq.example.com",
                            "fingerprint_sha256": "observed",
                            "matches_expected": False,
                        }
                    ],
                },
            },
        )

        result = operation_summary(operation)

        self.assertEqual(
            result["headline"], "One consumer serves the previous certificate."
        )
        self.assertEqual(result["condition"]["reason"], "VerificationFailed")
        self.assertEqual(result["affected"][0]["domain"], "hq.example.com")
        self.assertTrue(result["automatic"])
        json.dumps(get_managed_resource("certificate"))
