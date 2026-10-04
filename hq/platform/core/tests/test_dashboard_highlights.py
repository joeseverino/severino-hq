"""The public dashboard composes contributions without knowing their domains."""

from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.application.dashboard import dashboard_highlights
from hq.platform.application.domains import Domain, domain_dashboard_cards
from hq.platform.application.plugins import NavigationItem, PluginIntegration
from hq.platform.application.projection import projection_scope
from hq.platform.application.drawings import Dot, Dots, Trend
from hq.platform.application.ui import DomainOverview, Kpi


def contributor(number, *, overview=None):
    return Domain(
        id=f"example.section{number}",
        label=f"Example {number}",
        origin="extension",
        navigation=(NavigationItem(f"Example {number}", "dashboard", "", number),),
        integration=PluginIntegration(
            dashboard=Mock(
                return_value=(
                    {
                        "id": f"example-{number}-a",
                        "label": "First",
                        "value": 1,
                        "url": "/",
                    },
                    {
                        "id": f"example-{number}-b",
                        "label": "Second",
                        "value": 2,
                        "url": "/",
                    },
                )
            ),
            overview=overview,
        ),
    )


class DashboardHighlightTests(SimpleTestCase):
    def test_grouping_preserves_attribution_and_reads_cards_once_per_projection(self):
        domain = contributor(1)
        with (
            patch("hq.platform.application.domains.all_domains", return_value=(domain,)),
            patch("hq.platform.application.dashboard.all_domains", return_value=(domain,)),
            projection_scope(),
        ):
            cards = domain_dashboard_cards()
            highlights = dashboard_highlights()
        domain.integration.dashboard.assert_called_once()
        self.assertEqual(highlights["highlights"][0]["label"], "Example 1")
        self.assertEqual(highlights["highlights"][0]["cards"], cards)

    def test_rich_readings_keep_the_contributors_window_and_plain_links(self):
        overview = DomainOverview(
            "A useful summary",
            "/example/",
            (
                Kpi("Recent work", 12, "Through Aug 21, 2026", "/example/"),
                Kpi("Another reading", 3),
            ),
        )
        domain = contributor(1, overview=Mock(return_value=overview))
        with (
            patch("hq.platform.application.domains.all_domains", return_value=(domain,)),
            patch("hq.platform.application.dashboard.all_domains", return_value=(domain,)),
            projection_scope(),
        ):
            result = dashboard_highlights()
        html = render_to_string(
            "core/_dashboard_highlights.html",
            {"dashboard_highlights": result["highlights"]},
        )
        self.assertIn("Through Aug 21, 2026", html)
        self.assertIn('href="/example/"', html)
        self.assertNotIn("First</span>", html)

    def test_cross_domain_card_collisions_are_still_rejected(self):
        domain = contributor(1)
        with patch("hq.platform.application.domains.all_domains", return_value=(domain, domain)):
            with self.assertRaises(ImproperlyConfigured):
                domain_dashboard_cards()

    def test_one_metric_stays_compact_and_does_not_invoke_an_overview(self):
        overview = Mock()
        domain = contributor(1, overview=overview)
        domain.integration.dashboard.return_value = (
            domain.integration.dashboard.return_value[:1]
        )
        with (
            patch("hq.platform.application.domains.all_domains", return_value=(domain,)),
            patch("hq.platform.application.dashboard.all_domains", return_value=(domain,)),
        ):
            result = dashboard_highlights()
        overview.assert_not_called()
        self.assertFalse(result["highlights"])
        self.assertEqual(len(result["compact"]), 1)


class FigureDrawingTests(SimpleTestCase):
    """What a figure looks like beside its value: dots for a part, a line for a direction."""

    def render(self, *items):
        return render_to_string("partials/_kpi_grid.html", {"items": items})

    # ----- dots

    def test_a_count_of_a_whole_is_one_dot_each_filled_first(self):
        drawing = Dots.of(3, 5)
        html = self.render(Kpi("Machines", "3", "2 offline", drawing=drawing))

        self.assertEqual([dot.filled for dot in drawing.shown], [True, True, True, False, False])
        self.assertEqual(html.count("kpi-dot is-filled"), 3)
        self.assertEqual(html.count("<i "), 5)
        # Decoration: the note already says it, so it is not announced.
        self.assertIn('<span class="kpi-dots" aria-hidden="true">', html)
        self.assertIn("2 offline", html)

    def test_a_whole_too_large_to_count_at_a_glance_draws_no_dots(self):
        for drawing in (Dots.of(40, 90), Dots.of(0, 0)):
            self.assertEqual(drawing.shown, ())
            self.assertNotIn("kpi-dots", self.render(Kpi("Records", "40", drawing=drawing)))

    def test_a_part_larger_than_its_whole_is_refused(self):
        for filled, whole in ((6, 5), (-1, 5)):
            with self.assertRaises(ValueError):
                Dots.of(filled, whole)

    def test_a_dot_that_is_a_thing_names_itself_and_leads_to_its_page(self):
        drawing = Dots(
            (
                Dot(False, "beta · 192.0.2.2 · offline", "/machines/beta/"),
                Dot(True, "alpha · 192.0.2.1 · online", "/machines/alpha/"),
            )
        )
        html = self.render(Kpi("Machines", "1", url="/machines/", drawing=drawing))

        # Filled first, whatever order they were given in.
        self.assertEqual([dot.tip.split(" ")[0] for dot in drawing.shown], ["alpha", "beta"])
        self.assertIn(
            'href="/machines/alpha/" aria-label="alpha · 192.0.2.1 · online" data-tip="alpha · 192.0.2.1 · online"',
            html,
        )
        # A link cannot hold links: the cell stops being one, and the value
        # carries the figure's own page instead.
        self.assertIn('<div class="kpi">', html)
        self.assertIn('<a class="value" href="/machines/">1</a>', html)
        # Links are announced, so these dots are not hidden from a reader.
        self.assertIn('<span class="kpi-dots">', html)

    def test_dots_that_lead_nowhere_leave_the_figure_one_link(self):
        html = self.render(Kpi("Machines", "3", url="/machines/", drawing=Dots.of(3, 5)))

        self.assertIn('<a class="kpi" href="/machines/">', html)
        self.assertIn('<span class="value">3</span>', html)

    # ----- trend

    def test_a_trend_is_a_line_from_the_oldest_reading_to_the_newest(self):
        points = Trend((1.0, 2.0, 3.0)).points.split()

        # Left to right across the box, and rising: SVG's y grows downward.
        self.assertEqual([point.split(",")[0] for point in points], ["0.0", "50.0", "100.0"])
        heights = [float(point.split(",")[1]) for point in points]
        self.assertEqual(heights, sorted(heights, reverse=True))
        self.assertTrue(all(0 <= height <= Trend.HEIGHT for height in heights))

    def test_one_reading_or_none_draws_no_line(self):
        for values in ((), (4.0,)):
            drawing = Trend(values)
            self.assertEqual(drawing.points, "")
            self.assertNotIn("kpi-trend", self.render(Kpi("Balance", "$4", drawing=drawing)))

    def test_a_flat_trend_sits_mid_box_rather_than_dividing_by_zero(self):
        points = Trend((2.0, 2.0)).points.split()

        self.assertEqual({point.split(",")[1] for point in points}, {"12.0"})

    def test_every_reading_answers_when_pointed_at_in_the_domains_words(self):
        drawing = Trend((1.0, 3.0), ("Sep 1 · $1", "Sep 2 · $3"))
        html = self.render(Kpi("Balance", "$3", drawing=drawing))

        self.assertIn('data-tip="Sep 1 · $1"', html)
        self.assertIn('data-tip="Sep 2 · $3"', html)
        # The strips meet in the middle and cover the box between them.
        self.assertEqual(
            [(mark["left"], mark["width"]) for mark in drawing.marks],
            [("0.0", "50.0"), ("50.0", "50.0")],
        )
        # The newest reading keeps its dot; the others show theirs when pointed at.
        self.assertEqual(html.count("is-latest"), 1)
        # The line is decoration beside a figure that says the number.
        self.assertIn('class="kpi-trend"', html)
        self.assertIn('aria-hidden="true"', html)

    def test_a_reading_without_words_names_its_number(self):
        self.assertEqual([mark["tip"] for mark in Trend((4.0, 5.5)).marks], ["4", "5.5"])

    def test_words_for_some_readings_but_not_all_are_refused(self):
        with self.assertRaises(ValueError):
            Trend((1.0, 3.0), ("Sep 1 · $1",))

    # ----- the figure

    def test_a_figure_has_one_drawing_or_none(self):
        """One field, so a figure cannot be asked to be two things at once."""

        plain = self.render(Kpi("Domains", "4"))

        self.assertNotIn("kpi-dots", plain)
        self.assertNotIn("kpi-trend", plain)
        self.assertIn('<span class="value">4</span>', plain)

    def test_a_card_given_as_a_mapping_still_renders(self):
        html = self.render({"label": "First", "value": 1, "url": "/"})

        self.assertIn('<a class="kpi" href="/">', html)
        self.assertIn('<span class="value">1</span>', html)
        self.assertNotIn("kpi-dots", html)
        self.assertNotIn("kpi-trend", html)

    def test_an_icon_goes_before_the_label_and_an_unknown_one_draws_nothing(self):
        known = self.render(Kpi("Domains", "4", icon="globe"))
        unknown = self.render(Kpi("Domains", "4", icon="no-such-icon"))

        self.assertRegex(known, r'<span class="label">\s*<svg class="icon"[^>]*>.*<circle')
        self.assertNotIn("<circle", unknown)
        self.assertIn("Domains", unknown)


class DashboardHighlightQueryTests(TestCase):
    def test_overview_queries_grow_once_per_contributor_not_per_metric(self):
        def overview():
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return DomainOverview("Example", "/", (Kpi("Reading", 1),))

        for count in (1, 8):
            domains = tuple(contributor(i, overview=overview) for i in range(count))
            with (
                patch("hq.platform.application.domains.all_domains", return_value=domains),
                patch("hq.platform.application.dashboard.all_domains", return_value=domains),
                projection_scope(),
                CaptureQueriesContext(connection) as queries,
            ):
                domain_dashboard_cards()
                dashboard_highlights()
            self.assertEqual(len(queries), count)
            for domain in domains:
                domain.integration.dashboard.assert_called_once()
