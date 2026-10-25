from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderConnection,
    WeatherObservation,
)
from hq.platform.core.models import AuditLog
from hq.platform.core.tests.estate_sizes import assert_subject_cost_does_not_grow

from .. import readings
from ..glance import (
    _glance_reading,
    dashboard_panels,
    dashboard_refresh_plan,
    panel_specs,
    record_dashboard_observations,
    request_dashboard_refresh,
    save_dashboard_settings,
    select_dashboard_machine,
)
from ..security import cli_principal
from .markup import hrefs, select, text, with_class


class DashboardGlanceTests(TestCase):
    def test_a_stored_label_is_shown_under_its_display_label(self):
        metric = {"label": "Container CPU", "value": "12%", "detail": "Scope"}
        reading = _glance_reading(metric, panel_specs()[0])
        self.assertEqual(reading["label"], "CPU")
        self.assertEqual(reading["detail"], "Scope")
        self.assertEqual(metric["label"], "Container CPU")

    def test_the_head_leads_with_containers_and_says_the_share_under_it(self):
        readings.record(
            readings.machine_telemetry(self.machine.key),
            {
                "summary": "8 cores · 16 GB memory",
                "metrics": [
                    {"label": "Containers", "value": "12", "detail": "running"},
                    {"label": "CPU", "value": "1%", "detail": "of 8 cores"},
                    {"label": "Memory", "value": "4%", "detail": "583 MB of 16 GB"},
                ],
            },
        )
        panel = self.machine_panel()

        self.assertEqual((panel["lead"]["label"], panel["lead"]["value"]), ("Containers", "12"))
        self.assertEqual({(m["label"], m["value"]) for m in panel["caption"]}, {("CPU", "1%"), ("Memory", "4%")})
        shown = text(render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [panel]}))
        self.assertIn("583 MB of 16 GB", shown)
        self.assertIn("8 cores · 16 GB memory", shown)

    def test_a_machines_head_names_it_beside_another_machines(self):
        other = ManagedResource.objects.create(key="db-server", kind="machine", spec={"name": "db"})
        DashboardMachine.objects.create(machine=other, position=1)
        for machine in (self.machine, other):
            readings.record(
                readings.machine_telemetry(machine.key),
                {"metrics": [{"label": "CPU", "value": "37%"}, {"label": "Memory", "value": "81%"}]},
            )
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": dashboard_panels()})

        for machine, name in ((self.machine, "app"), (other, "db")):
            (part,) = select(html, "data-fragment-part", f"machine-{machine.pk}")
            (head,) = (element for element in with_class(html, "glance-caption") if part in element.ancestors)
            self.assertIn(name, head.text)

    def test_compact_summary_preserves_full_readings_in_native_details(self):
        readings.record(
            readings.machine_telemetry(self.machine.key),
            {
                "metrics": [
                    {"label": "CPU", "value": "12%"},
                    {"label": "Memory", "value": "579 MB"},
                    {"label": "Storage", "value": "3 GB"},
                ],
            },
        )
        panel = self.machine_panel()
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [panel]})

        self.assertEqual((panel["lead"]["label"], panel["lead"]["value"]), ("CPU", "12%"))
        (part,) = select(html, "data-fragment-part", panel["id"])
        self.assertEqual(part.tag, "details")
        for metric in ("12%", "Memory", "579 MB", "Storage", "3 GB"):
            self.assertIn(metric, part.text)

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

    def machine_panel(self):
        return next(panel for panel in dashboard_panels() if panel["id"] == self.machine_request_id)

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
        self.assertNotIn("telemetry", self.machine.status)
        telemetry = readings.stored(readings.machine_telemetry(self.machine.key))
        self.assertEqual(telemetry.value["metrics"][0]["value"], "12%")
        self.assertEqual(telemetry.value["controller_id"], "app-server")
        # A reading is not a change to the machine, so it is not an audit event.
        self.assertFalse(
            AuditLog.objects.filter(object_id=str(self.machine.pk), action=AuditLog.Action.UPDATED).exists()
        )
        self.assertIsNotNone(DashboardRefreshRequest.objects.get(panel_id=self.machine_request_id).completed_at)

    def _weather(self, **reading):
        DashboardRefreshRequest.objects.update_or_create(panel_id="weather", defaults={"completed_at": None})
        record_dashboard_observations(
            [{"panel_id": "weather", "point": "41.0000,-87.0000", **reading}],
            principal=cli_principal(),
            controller_id="app-server",
        )
        return WeatherObservation.objects.get(point="41.0000,-87.0000")

    GOOD = {"status": "good", "summary": "A town", "metrics": [{"label": "Now", "value": "Clear"}]}
    FAILED = {
        "status": "serious",
        "summary": "Refresh failed (HTTPError).",
        "metrics": [],
        "refresh_failed": "HTTPError",
    }

    def test_a_failed_refresh_keeps_the_last_good_reading_and_its_time(self):
        good = self._weather(**self.GOOD)

        kept = self._weather(**self.FAILED)

        self.assertEqual(kept.payload["metrics"], good.payload["metrics"])
        self.assertEqual(kept.payload["status"], "good")
        self.assertEqual(kept.payload["refresh_failed"], "HTTPError")
        self.assertEqual(kept.observed_at, good.observed_at)

    def test_the_next_good_refresh_clears_the_failure(self):
        self._weather(**self.GOOD)
        self._weather(**self.FAILED)

        fresh = self._weather(**self.GOOD)

        self.assertNotIn("refresh_failed", fresh.payload)

    def test_with_nothing_to_keep_the_failure_is_what_is_shown(self):
        failed = self._weather(**self.FAILED)

        self.assertEqual(failed.payload["status"], "serious")
        self.assertEqual(failed.payload["refresh_failed"], "HTTPError")

    def test_a_failure_marker_carries_only_the_error_type(self):
        failed = self._weather(**{**self.FAILED, "refresh_failed": "HTTPError: <b>/secret/path</b>"})

        self.assertEqual(failed.payload["refresh_failed"], "HTTPErrorbsecretpathb")

    def test_a_machine_keeps_its_last_good_telemetry_through_a_failed_refresh(self):
        def report(reading):
            DashboardRefreshRequest.objects.update_or_create(
                panel_id=self.machine_request_id, defaults={"completed_at": None}
            )
            record_dashboard_observations(
                [{"panel_id": "infrastructure", "machines": [{"key": "app-server", **reading}]}],
                principal=cli_principal(),
                controller_id="app-server",
            )
            return readings.stored(readings.machine_telemetry(self.machine.key))

        good = report({"status": "good", "summary": "", "metrics": [{"label": "CPU", "value": "3%"}]})
        seen_at = good.observed_at

        kept = report(self.FAILED)

        self.assertEqual(kept.value["metrics"][0]["value"], "3%")
        self.assertEqual(kept.value["refresh_failed"], "HTTPError")
        self.assertEqual(kept.observed_at, seen_at)

    def test_dashboard_projects_machine_and_weather_owners(self):
        observed = timezone.now()
        readings.record(
            readings.machine_telemetry(self.machine.key),
            {
                "status": "good",
                "summary": "Host load 0.10",
                "metrics": [{"label": "CPU", "value": "4%", "detail": ""}],
            },
            observed_at=observed,
        )
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

        self.assertEqual(panels[self.machine_request_id]["payload"]["summary"], "Host load 0.10")
        self.assertEqual(panels["weather"]["payload"]["summary"], "Chicago, IL")
        self.assertEqual(
            [metric["label"] for metric in panels["weather"]["payload"]["metrics"]],
            ["Now"],
        )

    def test_glance_query_cost_does_not_grow_with_machine_count(self):
        def arrange(count):
            ManagedResource.objects.filter(key__startswith="other-").delete()
            ManagedResource.objects.bulk_create(
                [
                    ManagedResource(key=f"other-{number}", kind="machine", spec={"name": f"other-{number}"})
                    for number in range(count)
                ]
            )

        # The glance reads the one machine it was asked about, never the rest.
        assert_subject_cost_does_not_grow(self, sizes=(1, 20), arrange=arrange, draw=lambda _count: dashboard_panels())

    def test_snapshot_age_is_explicit_at_the_freshness_boundary(self):
        """Due at one refresh interval; out of date only past the tolerance.

        A seven-minute-old reading is asked for again and is still current.
        """

        now = timezone.now()
        for age, due, stale in (
            (4, False, False),
            (5, True, False),
            (7, True, False),
            (15, True, True),
        ):
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
                with patch("hq.platform.application.glance.timezone.now", return_value=now):
                    panels = dashboard_panels()
                weather = next(panel for panel in panels if panel["id"] == "weather")
                self.assertEqual(weather["due"], due)
                self.assertEqual(weather["outdated"], stale)
                self.assertEqual(weather["payload"]["status"], "good")
                html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [weather]})
                self.assertEqual('data-reading-age="stale"' in html, stale)

    def test_refresh_preserves_old_readings_and_explains_pending_state(self):
        readings.record(
            readings.machine_telemetry(self.machine.key),
            {"metrics": [{"label": "CPU", "value": "4%"}]},
            observed_at=timezone.now() - timedelta(days=5),
        )
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)

        panels = dashboard_panels()
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": panels})

        # The reading stays up while it is replaced, marked as refreshing.
        machine = next(panel for panel in panels if panel["id"] == self.machine_request_id)
        self.assertTrue(machine["due"])
        (part,) = select(html, "data-fragment-part", self.machine_request_id)
        self.assertIn("data-refreshing", part.attrs)
        self.assertIn("4%", part.text)
        self.assertNotIn("Out of date", part.text)

    def test_with_no_machine_chosen_no_panel_is_drawn(self):
        # Get started's "Choose a machine" step asks instead; the header does
        # not repeat it.
        DashboardMachine.objects.all().delete()

        self.assertEqual(dashboard_panels(DashboardConfiguration(infrastructure_label="Homelab")), ())

    def test_never_observed_panels_are_not_reported_as_stale_readings(self):
        panels = dashboard_panels()
        self.assertTrue(all(not panel["due"] for panel in panels))
        self.assertTrue(all(panel["observed_at"] is None for panel in panels))

    def test_unknown_machine_is_rejected_and_not_created(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)
        with self.assertRaisesMessage(ValueError, "No machine HQ knows matched"):
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
            list(DashboardMachine.objects.values_list("machine__key", flat=True)),
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
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("hq.platform.application.glance.ring_doorbell") as ring,
        ):
            result = request_dashboard_refresh(principal=cli_principal())

        self.assertEqual(result["requested"], [self.machine_request_id])
        ring.assert_called_once_with()


class GlanceRenderingTests(TestCase):
    """The template renders what the panel data says, whatever the panel is."""

    def setUp(self):
        self.machine = ManagedResource.objects.create(key="app-server", kind="machine", spec={"name": "app"})
        DashboardMachine.objects.create(machine=self.machine)
        DashboardConfiguration.objects.create(
            weather_point="0.0000,0.0000",
            weather_label="Example weather",
            infrastructure_label="Example lab",
        )
        WeatherObservation.objects.create(
            point="0.0000,0.0000",
            payload={
                "status": "attention",
                "metrics": [
                    {"label": "Now", "value": "Clear"},
                    {"label": "High", "value": "20 C"},
                    {"label": "Alerts", "value": "2"},
                ],
            },
            observed_at=timezone.now(),
        )

    def test_the_weather_icon_follows_the_forecast_words(self):
        from hq.platform.application.glance import condition_icon

        for words, icon in (
            ("Chance Rain Showers", "rain"),
            ("Partly Cloudy", "weather"),
            ("Mostly Cloudy", "cloud"),
            ("Sunny", "sun"),
            ("Mostly Clear", "moon"),
            ("Haze", "weather"),
        ):
            with self.subTest(words=words):
                self.assertEqual(condition_icon(words, "weather"), icon)

    def test_a_forecast_hour_holds_only_what_the_dashboard_shows(self):
        from hq.platform.application.glance import _clean_panel

        cleaned = _clean_panel({"status": "good", "hours": [{"time": "4 PM", "temperature": "84°"}]})
        self.assertEqual(
            cleaned["hours"],
            [{"time": "4 PM", "temperature": "84°", "forecast": "", "precipitation": ""}],
        )
        with self.assertRaises(ValueError):
            _clean_panel({"status": "good", "hours": [{"time": "4 PM", "script": "x"}]})

    def test_icon_labels_and_alert_come_from_the_panel_spec(self):
        panels = {panel["id"]: panel for panel in dashboard_panels()}
        weather = panels["weather"]
        machine = panels[f"machine-{self.machine.pk}"]

        # "Clear" is the Weather Service's word for a clear night.
        self.assertEqual((weather["icon"], weather["head_labels"]), ("moon", False))
        self.assertEqual((machine["icon"], machine["head_labels"]), ("server", True))
        self.assertEqual(machine["label"], "Example lab")
        self.assertEqual(
            [(r["label"], r["short_label"], r["alert"]) for r in weather["readings"]],
            [
                ("Conditions", "Conditions", False),
                ("High", "High", False),
                ("Alerts", "Alerts", True),
            ],
        )

        self.assertEqual((weather["lead"]["label"], weather["alert"]), ("Conditions", "2 alerts"))
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [weather]})
        self.assertIn("2 alerts", text(html))

    def test_weather_leads_with_the_temperature_and_names_what_is_coming(self):
        WeatherObservation.objects.filter(point="0.0000,0.0000").update(
            payload={
                "summary": "Example Town, EX",
                "metrics": [
                    {"label": "Now", "value": "Mostly Cloudy"},
                    {"label": "Temperature", "value": "83°F"},
                    {"label": "Range", "value": "71–86°", "detail": "next 12 hours"},
                    {"label": "Rain", "value": "4 PM · 60%", "detail": "Showers"},
                    {"label": "Alerts", "value": "Heat Advisory", "detail": "Until 8 PM"},
                ],
                "hours": [
                    {"time": "4 PM", "temperature": "84°", "forecast": "Showers", "precipitation": "60%"},
                    {"time": "5 PM", "temperature": "82°", "forecast": "Cloudy", "precipitation": "0%"},
                ],
            }
        )
        weather = next(p for p in dashboard_panels() if p["id"] == "weather")
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [weather]})

        self.assertEqual((weather["lead"]["value"], weather["alert"]), ("83°F", "Heat Advisory"))
        self.assertLessEqual({"Mostly Cloudy", "4 PM · 60%", "71–86°"}, {m["value"] for m in weather["caption"]})
        self.assertIn("Example Town, EX", text(html))
        # A chance of rain is said only when there is one.
        hours = {hour.attrs["title"]: hour.text for hour in select(html, "title") if hour.tag == "li"}
        self.assertIn("60%", hours["Showers"])
        self.assertNotIn("0%", hours["Cloudy"])

    def test_a_zero_alert_count_is_dropped_by_the_spec_alert_metric(self):
        WeatherObservation.objects.filter(point="0.0000,0.0000").update(
            payload={"metrics": [{"label": "Alerts", "value": "0"}]}
        )
        weather = next(p for p in dashboard_panels() if p["id"] == "weather")

        self.assertEqual(weather["readings"], ())

    def test_no_machine_draws_no_machine_reading(self):
        DashboardMachine.objects.all().delete()
        spec = panel_specs()[0]

        self.assertNotIn("infrastructure", [panel["id"] for panel in dashboard_panels()])
        # Even when it would be the only reading: Get started asks for a machine.
        with patch("hq.platform.application.glance.panel_specs", return_value=(spec,)):
            self.assertEqual(dashboard_panels(), ())

    def test_the_settings_page_holds_the_form_and_its_placeholder_is_not_a_coordinate(self):
        self.client.force_login(get_user_model().objects.create_user("glance-settings"))
        response = self.client.get(reverse("dashboard_glance_settings"))

        (point,) = select(response, "name", "weather_point")
        self.assertNotRegex(point.attrs["placeholder"], r"\d")
        self.assertTrue(select(response, "name", "infrastructure_label"))

    def test_weather_waits_on_a_machine_a_controller_reaches(self):
        DashboardRefreshRequest.objects.create(panel_id="weather", requested_at=timezone.now())
        with patch("hq.platform.application.glance._machine_routes", return_value={}):
            weather = next(p for p in dashboard_panels() if p["id"] == "weather")
        self.assertEqual((weather["refreshable"], weather["refreshing"]), (False, False))

    def test_the_strip_has_no_controls_of_its_own(self):
        html = render_to_string(
            "core/_dashboard_glance.html",
            {"dashboard_panels": dashboard_panels(), "dashboard_can_refresh": True},
        )
        # Each panel's button posts through the strip's one form; settings are a page.
        (form,) = select(html, "id", "glance-refresh")
        self.assertEqual((form.tag, form.attrs["action"]), ("form", reverse("dashboard_glance")))
        self.assertTrue(all(button.tag == "button" for button in select(html, "form", "glance-refresh")))
        self.assertIn(reverse("dashboard_glance_settings"), hrefs(html))

    def test_machine_routes_read_the_catalog_once_per_projection(self):
        from ..projection import projection_scope

        with (
            patch("hq.platform.application.machines.machine_catalog", return_value=()) as catalog,
            projection_scope(),
        ):
            dashboard_panels()
            dashboard_panels()

        self.assertEqual(catalog.call_count, 1)


class GlanceEndpointTests(TestCase):
    """Reading the glance writes nothing; a POST asks for a refresh."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))
        self.machine = ManagedResource.objects.create(key="app-server", kind="machine", spec={"name": "app"})
        ProviderConnection.objects.create(
            connection_ref="app-ssh",
            controller_id="app-server",
            provider="ssh",
            endpoint="100.64.0.10",
            reaches=["app"],
            observed_at=timezone.now(),
        )
        DashboardMachine.objects.create(machine=self.machine)
        readings.record(
            readings.machine_telemetry(self.machine.key),
            {"metrics": [{"label": "CPU", "value": "4%"}]},
            observed_at=timezone.now() - timedelta(hours=1),
        )

    def test_a_get_on_a_stale_glance_requests_nothing(self):
        with patch("hq.platform.application.glance.ring_doorbell") as doorbell:
            response = self.client.get(reverse("dashboard_glance"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-reading-age="stale"')
        self.assertFalse(DashboardRefreshRequest.objects.exists())
        doorbell.assert_not_called()

    def test_the_dashboard_page_requests_nothing(self):
        with patch("hq.platform.application.glance.ring_doorbell"):
            self.client.get(reverse("dashboard"))

        self.assertFalse(DashboardRefreshRequest.objects.exists())

    def test_the_dashboard_reads_its_glance_inside_its_projection(self):
        from .. import machines

        with patch.object(machines, "machine_catalog", wraps=machines.machine_catalog) as catalog:
            self.client.get(reverse("dashboard"))

        self.assertEqual(catalog.call_count, 1)

    def test_a_stale_post_requests_the_stale_panels_and_shows_them_refreshing(self):
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("hq.platform.application.glance.ring_doorbell") as doorbell,
        ):
            response = self.client.post(reverse("dashboard_glance"), {"scope": "stale"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            list(DashboardRefreshRequest.objects.values_list("panel_id", flat=True)),
            [f"machine-{self.machine.pk}"],
        )
        (part,) = select(response, "data-fragment-part", f"machine-{self.machine.pk}")
        self.assertIn("data-refreshing", part.attrs)
        doorbell.assert_called_once()

    def test_a_stale_post_with_nothing_stale_requests_nothing(self):
        readings.record(
            readings.machine_telemetry(self.machine.key),
            {"metrics": [{"label": "CPU", "value": "4%"}]},
        )
        response = self.client.post(reverse("dashboard_glance"), {"scope": "stale"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DashboardRefreshRequest.objects.exists())
