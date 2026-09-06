from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderConnection,
    WeatherObservation,
)

from .glance import (
    _glance_reading,
    dashboard_panels,
    dashboard_refresh_plan,
    record_dashboard_observations,
    request_dashboard_refresh,
    save_dashboard_settings,
    select_dashboard_machine,
)
from .security import cli_principal


class DashboardGlanceTests(TestCase):
    def test_compact_meters_require_a_finite_bounded_percentage(self):
        for value, expected in (
            ("0%", 0),
            ("12.5%", 12.5),
            ("100%", 100),
            ("101%", None),
            ("-1%", None),
            ("NaN%", None),
            ("inf%", None),
            ("unknown%", None),
            ("579 MB", None),
        ):
            with self.subTest(value=value):
                metric = {"label": "Container CPU", "value": value, "detail": "Scope"}
                reading = _glance_reading(metric)
                self.assertEqual(reading["percent"], expected)
                self.assertEqual(reading["display_label"], "CPU")
                self.assertEqual(reading["detail"], "Scope")
                self.assertNotIn("percent", metric)

    def test_compact_summary_preserves_full_readings_in_native_details(self):
        self.machine.status = {
            "telemetry": {
                "metrics": [
                    {"label": "CPU", "value": "12%"},
                    {"label": "Memory", "value": "579 MB"},
                    {"label": "Storage", "value": "3 GB"},
                ],
            }
        }
        self.machine.save()
        html = render_to_string(
            "core/_dashboard_glance.html", {"dashboard_panels": dashboard_panels()}
        )
        self.assertIn('<details class="glance-panel', html)
        self.assertIn('<meter min="0" max="100" value="12.0" aria-label="CPU">', html)
        self.assertIn("Storage</dt>", html)
        self.assertIn("3 GB</dd>", html)
        self.assertNotIn('<meter min="0" max="100" value="579', html)

    def setUp(self):
        self.machine = ManagedResource.objects.create(
            key="app-server",
            kind="machine",
            spec={"name": "app", "addresses": ["100.64.0.10"]},
            status={"kept": True},
        )
        ProviderConnection.objects.create(
            connection_ref="app-ssh",
            controller_id="app-server",
            provider="ssh",
            endpoint="100.64.0.10",
            reaches=["app"],
            observed_at=timezone.now(),
        )
        DashboardConfiguration.objects.create(weather_point="41.0000,-87.0000")
        DashboardMachine.objects.create(machine=self.machine)

    @property
    def machine_request_id(self):
        return f"machine-{self.machine.pk}"

    def test_refresh_plan_names_the_record_and_derived_connection(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)

        plan = dashboard_refresh_plan("app-server")

        self.assertEqual(plan["panels"], ["infrastructure"])
        self.assertEqual(
            plan["targets"]["infrastructure"],
            [
                {
                    "key": "app-server",
                    "connections": ["app-ssh"],
                    "request_id": self.machine_request_id,
                }
            ],
        )

    def test_controller_reading_is_owned_by_the_machine_record(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)

        result = record_dashboard_observations(
            [
                {
                    "panel_id": "infrastructure",
                    "machines": [
                        {
                            "key": "app-server",
                            "status": "good",
                            "summary": "Host load 0.20",
                            "metrics": [
                                {
                                    "label": "CPU",
                                    "value": "12%",
                                    "detail": "8 cores",
                                }
                            ],
                        }
                    ],
                }
            ],
            principal=cli_principal(),
            controller_id="app-server",
        )

        self.machine.refresh_from_db()
        self.assertEqual(result["recorded"], ["infrastructure"])
        self.assertTrue(self.machine.status["kept"])
        self.assertEqual(self.machine.status["telemetry"]["metrics"][0]["value"], "12%")
        self.assertEqual(
            self.machine.status["telemetry"]["controller_id"], "app-server"
        )
        self.assertIsNotNone(self.machine.last_observed_at)
        self.assertIsNotNone(
            DashboardRefreshRequest.objects.get(
                panel_id=self.machine_request_id
            ).completed_at
        )

    def test_dashboard_projects_machine_and_weather_owners(self):
        observed = timezone.now()
        self.machine.status = {
            "telemetry": {
                "status": "good",
                "summary": "Host load 0.10",
                "metrics": [{"label": "CPU", "value": "4%", "detail": ""}],
            }
        }
        self.machine.last_observed_at = observed
        self.machine.save(update_fields=("status", "last_observed_at", "updated_at"))
        WeatherObservation.objects.create(
            point="41.0000,-87.0000",
            payload={
                "status": "good",
                "summary": "Chicago, IL",
                "metrics": [
                    {"label": "Now", "value": "Clear", "detail": ""},
                    {"label": "Alerts", "value": "0", "detail": ""},
                ],
            },
            observed_at=observed,
        )

        panels = {panel["id"]: panel for panel in dashboard_panels()}

        self.assertEqual(
            panels[self.machine_request_id]["payload"]["summary"], "Host load 0.10"
        )
        self.assertEqual(panels["weather"]["payload"]["summary"], "Chicago, IL")
        self.assertEqual(
            [metric["label"] for metric in panels["weather"]["payload"]["metrics"]],
            ["Now"],
        )

    def test_glance_query_cost_does_not_grow_with_machine_count(self):
        with CaptureQueriesContext(connection) as baseline:
            dashboard_panels()
        ManagedResource.objects.bulk_create(
            [
                ManagedResource(
                    key=f"other-{number}",
                    kind="machine",
                    spec={"name": f"other-{number}"},
                )
                for number in range(20)
            ]
        )

        with CaptureQueriesContext(connection) as expanded:
            dashboard_panels()

        self.assertEqual(len(expanded), len(baseline))

    def test_snapshot_age_is_explicit_at_the_freshness_boundary(self):
        now = timezone.now()
        for age, stale in ((59, False), (60, True), (61, True)):
            with self.subTest(age=age):
                WeatherObservation.objects.update_or_create(
                    point="41.0000,-87.0000",
                    defaults={
                        "payload": {
                            "status": "good",
                            "metrics": [{"label": "Now", "value": "Clear"}],
                        },
                        "observed_at": now - timedelta(minutes=age),
                    },
                )
                with patch("application.glance.timezone.now", return_value=now):
                    panels = dashboard_panels()
                weather = next(panel for panel in panels if panel["id"] == "weather")
                self.assertEqual(weather["stale"], stale)
                self.assertEqual(weather["payload"]["status"], "good")
                html = render_to_string(
                    "core/_dashboard_glance.html", {"dashboard_panels": [weather]}
                )
                self.assertEqual("Out of date" in html, stale)
                self.assertIn('datetime="', html)
                self.assertIn("Conditions</dt>", html)
                self.assertNotIn("Now</dt>", html)

    def test_refresh_preserves_old_readings_and_explains_pending_state(self):
        self.machine.status = {
            "telemetry": {"metrics": [{"label": "CPU", "value": "4%"}]}
        }
        self.machine.last_observed_at = timezone.now() - timedelta(days=5)
        self.machine.save()
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)

        panels = dashboard_panels()
        html = render_to_string(
            "core/_dashboard_glance.html", {"dashboard_panels": panels}
        )

        self.assertIn("Out of date", html)
        self.assertIn("Refreshing…", html)
        self.assertIn("4%", html)
        self.assertTrue(panels[0]["stale"])

    def test_never_observed_panels_are_not_reported_as_stale_readings(self):
        panels = dashboard_panels()
        self.assertTrue(all(not panel["stale"] for panel in panels))
        self.assertTrue(all(panel["observed_at"] is None for panel in panels))

    def test_unknown_machine_is_rejected_and_not_created(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)
        with self.assertRaisesMessage(ValueError, "No declared machine matched"):
            record_dashboard_observations(
                [
                    {
                        "panel_id": "infrastructure",
                        "machines": [
                            {
                                "key": "not-declared",
                                "status": "good",
                                "metrics": [{"label": "CPU", "value": "1%"}],
                            }
                        ],
                    }
                ],
                principal=cli_principal(),
                controller_id="app-server",
            )

        self.assertFalse(ManagedResource.objects.filter(key="not-declared").exists())

    def test_another_controller_cannot_claim_the_machine_refresh(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)

        plan = dashboard_refresh_plan("another-controller")

        self.assertEqual(plan["panels"], [])
        self.assertEqual(plan["targets"]["infrastructure"], [])

    def test_machine_selection_is_a_persisted_ui_setting(self):
        other = ManagedResource.objects.create(
            key="other-machine",
            kind="machine",
            spec={"name": "other-machine"},
        )

        select_dashboard_machine(other.key, selected=True, principal=cli_principal())

        self.assertEqual(
            list(
                DashboardMachine.objects.values_list("machine__key", flat=True)
            ),
            ["app-server", "other-machine"],
        )

    def test_weather_settings_validate_and_normalize_coordinates(self):
        save_dashboard_settings(
            weather_point=" 41.1, -87.2 ",
            weather_label="Outside",
            infrastructure_label="Lab",
            principal=cli_principal(),
        )

        configuration = DashboardConfiguration.objects.get()
        self.assertEqual(configuration.weather_point, "41.1000,-87.2000")
        self.assertEqual(configuration.weather_label, "Outside")
        self.assertEqual(configuration.infrastructure_label, "Lab")

    def test_invalid_status_is_rejected(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)
        with self.assertRaisesMessage(ValueError, "Unknown dashboard status"):
            record_dashboard_observations(
                [
                    {
                        "panel_id": "infrastructure",
                        "machines": [
                            {
                                "key": "app-server",
                                "status": "made-up",
                                "metrics": [{"label": "CPU", "value": "1%"}],
                            }
                        ],
                    }
                ],
                principal=cli_principal(),
                controller_id="app-server",
            )

    def test_unsolicited_observation_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "No refresh is pending"):
            record_dashboard_observations(
                [
                    {
                        "panel_id": "infrastructure",
                        "machines": [
                            {
                                "key": "app-server",
                                "status": "good",
                                "metrics": [{"label": "CPU", "value": "1%"}],
                            }
                        ],
                    }
                ],
                principal=cli_principal(),
                controller_id="app-server",
            )

    def test_refresh_is_explicit_and_rings_the_existing_doorbell(self):
        configuration = DashboardConfiguration.objects.get()
        configuration.weather_point = ""
        configuration.save(update_fields=("weather_point", "updated_at"))
        with self.captureOnCommitCallbacks(execute=True):
            from unittest.mock import patch

            with patch("application.glance.ring_doorbell") as ring:
                result = request_dashboard_refresh(principal=cli_principal())

        self.assertEqual(result["requested"], [self.machine_request_id])
        ring.assert_called_once_with()
