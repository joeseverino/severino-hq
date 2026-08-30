from __future__ import annotations

from django.db import connection
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
    dashboard_panels,
    dashboard_refresh_plan,
    record_dashboard_observations,
    request_dashboard_refresh,
    save_dashboard_settings,
    select_dashboard_machine,
)
from .security import cli_principal


class DashboardGlanceTests(TestCase):
    def setUp(self):
        self.machine = ManagedResource.objects.create(
            key="homelab-server",
            kind="machine",
            spec={"name": "homelab", "addresses": ["100.64.0.10"]},
            status={"kept": True},
        )
        ProviderConnection.objects.create(
            connection_ref="homelab-ssh",
            controller_id="homelab-server",
            provider="ssh",
            endpoint="100.64.0.10",
            reaches=["homelab"],
            observed_at=timezone.now(),
        )
        DashboardConfiguration.objects.create(weather_point="41.0000,-87.0000")
        DashboardMachine.objects.create(machine=self.machine)

    @property
    def machine_request_id(self):
        return f"machine-{self.machine.pk}"

    def test_refresh_plan_names_the_record_and_derived_connection(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)

        plan = dashboard_refresh_plan("homelab-server")

        self.assertEqual(plan["panels"], ["infrastructure"])
        self.assertEqual(
            plan["targets"]["infrastructure"],
            [
                {
                    "key": "homelab-server",
                    "connections": ["homelab-ssh"],
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
                            "key": "homelab-server",
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
            controller_id="homelab-server",
        )

        self.machine.refresh_from_db()
        self.assertEqual(result["recorded"], ["infrastructure"])
        self.assertTrue(self.machine.status["kept"])
        self.assertEqual(self.machine.status["telemetry"]["metrics"][0]["value"], "12%")
        self.assertEqual(
            self.machine.status["telemetry"]["controller_id"], "homelab-server"
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
                controller_id="homelab-server",
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
            ["homelab-server", "other-machine"],
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
                                "key": "homelab-server",
                                "status": "made-up",
                                "metrics": [{"label": "CPU", "value": "1%"}],
                            }
                        ],
                    }
                ],
                principal=cli_principal(),
                controller_id="homelab-server",
            )

    def test_unsolicited_observation_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "No refresh is pending"):
            record_dashboard_observations(
                [
                    {
                        "panel_id": "infrastructure",
                        "machines": [
                            {
                                "key": "homelab-server",
                                "status": "good",
                                "metrics": [{"label": "CPU", "value": "1%"}],
                            }
                        ],
                    }
                ],
                principal=cli_principal(),
                controller_id="homelab-server",
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
