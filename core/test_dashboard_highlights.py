"""The public dashboard composes contributions without knowing their domains."""

from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from application.dashboard import dashboard_highlights
from application.domains import Domain, domain_dashboard_cards
from application.plugins import NavigationItem, PluginIntegration
from application.projection import projection_scope
from application.ui import DomainOverview, Kpi


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
            patch("application.domains.all_domains", return_value=(domain,)),
            patch("application.dashboard.all_domains", return_value=(domain,)),
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
            patch("application.domains.all_domains", return_value=(domain,)),
            patch("application.dashboard.all_domains", return_value=(domain,)),
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
        with patch("application.domains.all_domains", return_value=(domain, domain)):
            with self.assertRaises(ImproperlyConfigured):
                domain_dashboard_cards()

    def test_one_metric_stays_compact_and_does_not_invoke_an_overview(self):
        overview = Mock()
        domain = contributor(1, overview=overview)
        domain.integration.dashboard.return_value = (
            domain.integration.dashboard.return_value[:1]
        )
        with (
            patch("application.domains.all_domains", return_value=(domain,)),
            patch("application.dashboard.all_domains", return_value=(domain,)),
        ):
            result = dashboard_highlights()
        overview.assert_not_called()
        self.assertFalse(result["highlights"])
        self.assertEqual(len(result["compact"]), 1)


class DashboardHighlightQueryTests(TestCase):
    def test_overview_queries_grow_once_per_contributor_not_per_metric(self):
        def overview():
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return DomainOverview("Example", "/", (Kpi("Reading", 1),))

        for count in (1, 8):
            domains = tuple(contributor(i, overview=overview) for i in range(count))
            with (
                patch("application.domains.all_domains", return_value=domains),
                patch("application.dashboard.all_domains", return_value=domains),
                projection_scope(),
                CaptureQueriesContext(connection) as queries,
            ):
                domain_dashboard_cards()
                dashboard_highlights()
            self.assertEqual(len(queries), count)
            for domain in domains:
                domain.integration.dashboard.assert_called_once()
