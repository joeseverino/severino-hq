import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderConnection,
)
from hq.platform.application.markup_testing import hrefs, select, text, with_class
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import declare, operator
from hq.platform.core.tests.estate_sizes import assert_subject_cost_does_not_grow
from hq_sdk.testing import plain

from .. import readings
from ..asks import REFRESHING
from ..capabilities import execute_capability
from ..glance import (
    DASHBOARD_MACHINE,
    DASHBOARD_SETTINGS,
    DashboardMachineCommand,
    dashboard_panels,
    panel_specs,
    select_dashboard_machine,
)
from ..glance_refresh import DASHBOARD_REFRESH, GLANCE_REGION, dashboard_refresh_plan, refresh_ask
from ..report_testing import bridged
from ..security import OPERATOR_CAPABILITIES, Capability, Principal, cli_principal
from .glance_readings import DOCKER, HOST, reported_hour, reported_machine, reported_weather

AN_OPERATOR = Principal("operator", "web", OPERATOR_CAPABILITIES)


def _now() -> str:
    """The as_of of a plan made just now."""

    return timezone.now().isoformat()


@contextmanager
def doorbells() -> Iterator[Callable[[], list[str]]]:
    """The host's doorbells in a directory of their own, and a way to ask
    which of them have been rung."""

    with (
        tempfile.TemporaryDirectory() as directory,
        override_settings(SEVERINO_CONTROLLER_DOORBELL=str(Path(directory) / "controller-doorbell")),
    ):
        yield lambda: sorted(path.name for path in Path(directory).iterdir())


# The weather point every test here configures.
POINT = "41.0000,-87.0000"
# A clear hour, the forecast's first.
CLEAR = reported_weather(POINT, reported_hour("2026-07-01T13:00:00-05:00", forecast="Clear"), city="A town")


class DashboardGlanceTests(TestCase):
    def test_the_head_leads_with_containers_and_says_the_share_under_it(self):
        readings.record(readings.machine_telemetry(self.machine.key), reported_machine("app-server", docker=DOCKER))
        panel = self.machine_panel()

        self.assertEqual((panel["lead"]["label"], panel["lead"]["value"]), ("Containers", "3"))
        self.assertEqual({(m["label"], m["value"]) for m in panel["caption"]}, {("CPU", "37%"), ("Memory", "25%")})
        shown = text(render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [panel]}))
        self.assertIn("2.0 GB of 8.0 GB", shown)
        self.assertIn("4 cores", shown)
        self.assertIn("3.0 GB", shown)

    def test_a_host_read_over_ssh_is_worded_from_its_numbers(self):
        readings.record(readings.machine_telemetry(self.machine.key), reported_machine("app-server", host=HOST))
        panel = self.machine_panel()

        self.assertEqual(
            [(m["label"], m["value"], m["detail"]) for m in panel["readings"]],
            [
                ("CPU", "9%", "8 cores"),
                ("Memory", "25%", "4.0 GB of 16.0 GB used"),
                ("Storage", "50%", "50.0 GB of 100.0 GB used on /"),
            ],
        )
        self.assertEqual((panel["payload"]["status"], panel["payload"]["summary"]), ("good", "Host load 0.30"))

    def test_a_machine_with_no_reading_says_so(self):
        readings.record(readings.machine_telemetry(self.machine.key), reported_machine("app-server"))
        panel = self.machine_panel()

        self.assertEqual(panel["payload"]["status"], "attention")
        self.assertEqual(panel["readings"], ())

    def test_a_reading_kept_in_an_older_shape_is_no_reading_and_falls_due(self):
        # As readings were stored while the controller worded them.
        observed = timezone.now() - timedelta(minutes=6)
        readings.record(
            readings.machine_telemetry(self.machine.key),
            {"status": "good", "summary": "Host load 0.20", "metrics": [{"label": "CPU", "value": "12%"}]},
            observed_at=observed,
        )
        panel = self.machine_panel()

        self.assertEqual((panel["payload"], panel["readings"]), ({}, ()))
        self.assertEqual(panel["observed_at"], observed)
        self.assertTrue(panel["due"])

    def test_a_machines_head_names_it_beside_another_machines(self):
        other = declare("db-server", "machine", {"name": "db"})
        DashboardMachine.objects.create(machine=other, position=1)
        for machine in (self.machine, other):
            readings.record(readings.machine_telemetry(machine.key), reported_machine(machine.key, host=HOST))
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": dashboard_panels()})

        for machine, name in ((self.machine, "app"), (other, "db")):
            (part,) = select(html, "data-fragment-part", f"machine-{machine.pk}")
            (head,) = (
                element
                for element in with_class(html, "fact-label")
                if part in element.ancestors and not any("fact-panel" in up.classes for up in element.ancestors)
            )
            self.assertIn(name, head.text)

    def test_compact_summary_preserves_full_readings_in_the_menu_it_opens(self):
        readings.record(readings.machine_telemetry(self.machine.key), reported_machine("app-server", host=HOST))
        panel = self.machine_panel()
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [panel]})

        self.assertEqual((panel["lead"]["label"], panel["lead"]["value"]), ("CPU", "9%"))
        (opener,) = select(html, "popovertarget")
        (opened,) = select(html, "id", opener.attrs["popovertarget"])
        self.assertIn("popover", opened.attrs)
        for metric in ("9%", "Memory", "4.0 GB of 16.0 GB used", "Storage", "50%"):
            self.assertIn(metric, plain(opened.text))

    def setUp(self):
        self.machine = declare(
            "app-server", "machine", {"name": "app", "addresses": ["100.64.0.10"]}, status={"kept": True}
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

        result = bridged(
            "glance",
            [
                {
                    "panel_id": "infrastructure",
                    "as_of": _now(),
                    "machines": [reported_machine("app-server", host=HOST)],
                }
            ],
            controller_id="app-server",
        )

        self.machine.refresh_from_db()
        self.assertEqual(result["recorded"], ["infrastructure"])
        self.assertTrue(self.machine.status["kept"])
        self.assertNotIn("telemetry", self.machine.status)
        telemetry = readings.stored(readings.machine_telemetry(self.machine.key))
        self.assertEqual(telemetry.value, reported_machine("app-server", host=HOST))
        # A reading is not a change to the machine, so it is not an audit event.
        self.assertFalse(
            AuditLog.objects.filter(object_id=str(self.machine.pk), action=AuditLog.Action.UPDATED).exists()
        )
        self.assertIsNotNone(DashboardRefreshRequest.objects.get(panel_id=self.machine_request_id).completed_at)

    def _weather(self, reading):
        DashboardRefreshRequest.objects.update_or_create(panel_id="weather", defaults={"completed_at": None})
        bridged("glance", [{"panel_id": "weather", "as_of": _now(), "weather": reading}], controller_id="app-server")
        return readings.stored(readings.weather(POINT))

    FAILED = reported_weather(POINT, refresh_failed="network")

    def weather_panel(self):
        return next(panel for panel in dashboard_panels() if panel["id"] == "weather")

    def test_a_failed_refresh_keeps_the_last_good_reading_and_its_time(self):
        good = self._weather(CLEAR)

        kept = self._weather(self.FAILED)

        self.assertEqual(kept.value["hours"], good.value["hours"])
        self.assertEqual(kept.value["refresh_failed"], "network")
        self.assertEqual(kept.observed_at, good.observed_at)
        panel = self.weather_panel()
        self.assertEqual(panel["payload"]["status"], "good")
        self.assertEqual(panel["payload"]["refresh_failed"], "network")

    def test_the_next_good_refresh_clears_the_failure(self):
        self._weather(CLEAR)
        self._weather(self.FAILED)

        fresh = self._weather(CLEAR)

        self.assertNotIn("refresh_failed", fresh.value)

    def test_with_nothing_to_keep_the_failure_is_what_is_shown(self):
        failed = self._weather(self.FAILED)

        self.assertEqual(failed.value["refresh_failed"], "network")
        panel = self.weather_panel()
        self.assertEqual((panel["payload"]["status"], panel["readings"]), ("serious", ()))

    def test_a_report_outside_the_contracts_shape_is_refused(self):
        for reading in (
            {**CLEAR, "summary": "Words the controller chose"},
            {**CLEAR, "hours": [{"time": "4 PM", "temperature": "84°"}]},
        ):
            with self.subTest(reading=reading), self.assertRaisesMessage(ValueError, "not the shape the contract"):
                self._weather(reading)
        self.assertIsNone(readings.stored(readings.weather(POINT)))

    def test_a_machine_keeps_its_last_good_telemetry_through_a_failed_refresh(self):
        def report(reading):
            DashboardRefreshRequest.objects.update_or_create(
                panel_id=self.machine_request_id, defaults={"completed_at": None}
            )
            bridged(
                "glance",
                [{"panel_id": "infrastructure", "as_of": _now(), "machines": [{"key": "app-server", **reading}]}],
                controller_id="app-server",
            )
            return readings.stored(readings.machine_telemetry(self.machine.key))

        good = report({"host": HOST})
        seen_at = good.observed_at

        kept = report({"refresh_failed": "credential"})

        self.assertEqual(kept.value["host"], HOST)
        self.assertEqual(kept.value["refresh_failed"], "credential")
        self.assertEqual(kept.observed_at, seen_at)

    def test_dashboard_projects_machine_and_weather_owners(self):
        observed = timezone.now()
        readings.record(
            readings.machine_telemetry(self.machine.key),
            reported_machine("app-server", host=HOST),
            observed_at=observed,
        )
        readings.record(readings.weather(POINT), {**CLEAR, "city": "Chicago", "state": "IL"}, observed_at=observed)

        panels = {panel["id"]: panel for panel in dashboard_panels()}

        self.assertEqual(panels[self.machine_request_id]["payload"]["summary"], "Host load 0.30")
        self.assertEqual(panels["weather"]["payload"]["summary"], "Chicago, IL")
        # No alert is in force, so there is no alert to name.
        self.assertNotIn("Alerts", [metric["label"] for metric in panels["weather"]["payload"]["metrics"]])

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
                readings.record(readings.weather(POINT), CLEAR, observed_at=now - timedelta(minutes=age))
                with patch("hq.platform.application.glance.timezone.now", return_value=now):
                    panels = dashboard_panels()
                weather = next(panel for panel in panels if panel["id"] == "weather")
                self.assertEqual(weather["due"], due)
                self.assertEqual(weather["outdated"], stale)
                self.assertEqual(weather["payload"]["status"], "good")
                html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [weather]})
                self.assertEqual('data-reading-age="stale"' in html, stale)

    def test_with_no_machine_chosen_no_panel_is_drawn(self):
        # Get started's "Choose a machine" step asks instead; the header does
        # not repeat it.
        DashboardMachine.objects.all().delete()

        self.assertEqual(dashboard_panels(DashboardConfiguration(infrastructure_label="Homelab")), ())

    def test_unknown_machine_is_rejected_and_not_created(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)
        with self.assertRaisesMessage(ValueError, "No machine HQ knows matched"):
            bridged(
                "glance",
                [
                    {
                        "panel_id": "infrastructure",
                        "as_of": _now(),
                        "machines": [reported_machine("not-declared", host=HOST)],
                    }
                ],
                controller_id="app-server",
            )

        self.assertFalse(ManagedResource.objects.filter(key="not-declared").exists())

    def test_another_controller_cannot_claim_the_machine_refresh(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)

        plan = dashboard_refresh_plan("another-controller")

        self.assertEqual(plan["panels"], [])
        self.assertEqual(plan["targets"]["infrastructure"], [])

    def test_machine_selection_is_a_persisted_ui_setting(self):
        other = declare("other-machine", "machine", {"name": "other-machine"})

        select_dashboard_machine(DashboardMachineCommand(selected=True), principal=AN_OPERATOR, current_key=other.key)

        self.assertEqual(
            list(DashboardMachine.objects.values_list("machine__key", flat=True)),
            ["app-server", "other-machine"],
        )

    def test_the_machine_form_chooses_it_audited_under_the_operator(self):
        operator(self.client)
        other = declare("other-machine", "machine", {"name": "other-machine"})

        response = self.client.post(
            reverse("control_plane:edit", kwargs={"key": other.key}),
            form_keyed({"name": "other-machine", "enabled": "on", "selected": "on"}),
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(DashboardMachine.objects.filter(machine=other).exists())
        shown = AuditLog.objects.get(object_type="Dashboard", metadata__subject=other.key)
        self.assertEqual((shown.metadata["actor"], shown.metadata["operation"]), ("operator", DASHBOARD_MACHINE))

    def test_only_a_person_in_the_browser_chooses_the_dashboard_machines(self):
        other = declare("other-machine", "machine", {"name": "other-machine"})
        granted = frozenset(Capability)
        for principal in (
            Principal("example-agent", "mcp", granted, granted=granted),
            Principal("example-client", "api", granted, granted=granted),
            cli_principal(),
        ):
            with self.subTest(interface=principal.interface):
                result = execute_capability(
                    DASHBOARD_MACHINE, keyed({"selected": True}), principal=principal, target=other.key
                )

                self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(DashboardMachine.objects.filter(machine=other).exists())
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.DENIED).count(), 3)

    def test_a_machine_switched_off_is_a_precondition_and_a_key_naming_no_machine_is_not_found(self):
        declare("off-machine", "machine", {"name": "off"}, enabled=False)
        declare("example-zone", "cloudflare.zone", {"name": "example.com"})
        for key, code in (
            ("off-machine", "precondition"),
            ("nothing-by-this-name", "not_found"),
            ("example-zone", "not_found"),
        ):
            with self.subTest(key=key):
                result = execute_capability(
                    DASHBOARD_MACHINE, keyed({"selected": True}), principal=AN_OPERATOR, target=key
                )

                self.assertEqual(result["error"]["code"], code, result)
        self.assertEqual(DashboardMachine.objects.count(), 1)

    def test_a_selection_that_is_not_a_yes_or_no_is_invalid(self):
        for payload in ({}, {"selected": "sometimes"}):
            with self.subTest(payload=payload):
                result = execute_capability(
                    DASHBOARD_MACHINE, keyed(payload), principal=AN_OPERATOR, target="app-server"
                )

                self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertEqual(DashboardMachine.objects.count(), 1)

    def test_weather_settings_validate_and_normalize_coordinates(self):
        answer = execute_capability(
            DASHBOARD_SETTINGS,
            keyed({"weather_point": " 41.1, -87.2 ", "weather_label": "Outside", "infrastructure_label": "Lab"}),
            principal=AN_OPERATOR,
        )
        self.assertTrue(answer["ok"], answer)

        configuration = DashboardConfiguration.objects.get()
        self.assertEqual(configuration.weather_point, "41.1000,-87.2000")
        self.assertEqual(configuration.weather_label, "Outside")
        self.assertEqual(configuration.infrastructure_label, "Lab")

    def test_a_label_longer_than_its_column_is_refused_never_cut(self):
        for field in ("weather_label", "infrastructure_label"):
            with self.subTest(field=field):
                answer = execute_capability(DASHBOARD_SETTINGS, keyed({field: "x" * 41}), principal=AN_OPERATOR)

                self.assertEqual(answer["error"]["code"], "invalid_input")
                self.assertEqual(answer["error"]["details"][0]["loc"], [field])
                self.assertFalse(DashboardConfiguration.objects.filter(**{field: "x" * 40}).exists())

    def test_unsolicited_observation_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "No refresh is pending"):
            bridged(
                "glance",
                [
                    {
                        "panel_id": "infrastructure",
                        "as_of": _now(),
                        "machines": [reported_machine("app-server", host=HOST)],
                    }
                ],
                controller_id="app-server",
            )

    def _report_machine(self, as_of):
        return bridged(
            "glance",
            [{"panel_id": "infrastructure", "as_of": as_of, "machines": [reported_machine("app-server", host=HOST)]}],
            controller_id="app-server",
        )

    def test_a_request_made_while_a_reading_was_under_way_waits_for_the_next(self):
        now = timezone.now()
        DashboardRefreshRequest.objects.create(
            panel_id=self.machine_request_id, requested_at=now - timedelta(minutes=2)
        )
        # The controller took its plan a minute ago, and the panel was asked
        # for again while it read.
        planned = (now - timedelta(minutes=1)).isoformat()
        DashboardRefreshRequest.objects.filter(panel_id=self.machine_request_id).update(
            requested_at=now - timedelta(seconds=30)
        )

        self._report_machine(planned)

        # The reading is kept, but it began before the second request.
        self.assertIsNotNone(readings.stored(readings.machine_telemetry(self.machine.key)))
        self.assertIsNone(DashboardRefreshRequest.objects.get(panel_id=self.machine_request_id).completed_at)

        self._report_machine(dashboard_refresh_plan("app-server")["as_of"])

        self.assertIsNotNone(DashboardRefreshRequest.objects.get(panel_id=self.machine_request_id).completed_at)

    def test_an_observation_that_does_not_say_when_its_plan_was_made_is_refused(self):
        DashboardRefreshRequest.objects.create(panel_id=self.machine_request_id)

        for as_of in (None, "", "yesterday", "2026-01-01T00:00:00"):
            with self.subTest(as_of=as_of), self.assertRaisesMessage(ValueError, "does not say when its plan was made"):
                self._report_machine(as_of)

        self.assertIsNone(DashboardRefreshRequest.objects.get(panel_id=self.machine_request_id).completed_at)

    def test_a_refresh_rings_the_glance_doorbell(self):
        configuration = DashboardConfiguration.objects.get()
        configuration.weather_point = ""
        configuration.save(update_fields=("weather_point", "updated_at"))
        with doorbells() as rung:
            with self.captureOnCommitCallbacks(execute=True):
                result = execute_capability(DASHBOARD_REFRESH, {}, principal=cli_principal())

            # One rung on the controller's doorbell during a sweep waits for
            # that sweep to end; the glance's starts a pass of its own.
            self.assertIn("glance-doorbell", rung())
        self.assertEqual(result["requested"], [self.machine_request_id])


class GlanceRenderingTests(TestCase):
    """The template renders what the panel data says, whatever the panel is."""

    def setUp(self):
        self.machine = declare("app-server", "machine", {"name": "app"})
        DashboardMachine.objects.create(machine=self.machine)
        DashboardConfiguration.objects.create(
            weather_point="0.0000,0.0000",
            weather_label="Example weather",
            infrastructure_label="Example lab",
        )
        readings.record(
            readings.weather("0.0000,0.0000"),
            reported_weather(
                "0.0000,0.0000",
                reported_hour("2026-07-01T01:00:00+00:00", temperature=None, forecast="Clear"),
                alerts=[{"event": "Wind Advisory", "severity": "Moderate"}, {"event": "Other"}],
            ),
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

    def test_icon_labels_and_alert_come_from_the_panel_spec(self):
        panels = {panel["id"]: panel for panel in dashboard_panels()}
        weather = panels["weather"]
        machine = panels[f"machine-{self.machine.pk}"]

        # "Clear" is the Weather Service's word for a clear night.
        self.assertEqual((weather["icon"], weather["head_labels"]), ("moon", False))
        self.assertEqual((machine["icon"], machine["head_labels"]), ("server", True))
        self.assertEqual(machine["label"], "Example lab")
        # An hour with no temperature says none, rather than a mark for it.
        self.assertEqual(
            [(r["label"], r["short_label"], r["alert"]) for r in weather["readings"]],
            [("Conditions", "Conditions", False), ("Alerts", "Alerts", True)],
        )
        # The alerts' tone is the most severe of them.
        self.assertEqual(weather["payload"]["status"], "attention")

        self.assertEqual((weather["lead"]["label"], weather["alert"]), ("Conditions", "Wind Advisory +1"))
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [weather]})
        self.assertIn("Wind Advisory +1", text(html))

    @override_settings(TIME_ZONE="America/Chicago")
    def test_weather_leads_with_the_temperature_and_names_what_is_coming(self):
        readings.record(
            readings.weather("0.0000,0.0000"),
            reported_weather(
                "0.0000,0.0000",
                reported_hour("2026-07-01T20:00:00+00:00", 83, forecast="Mostly Cloudy"),
                reported_hour("2026-07-01T21:00:00+00:00", 86, forecast="Showers", chance=60),
                reported_hour("2026-07-01T22:00:00+00:00", 71.5, forecast="Cloudy"),
                city="Example Town",
                state="EX",
                alerts=[{"event": "Heat Advisory", "headline": "Until 8 PM", "severity": "Severe"}],
            ),
        )
        weather = next(p for p in dashboard_panels() if p["id"] == "weather")
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": [weather]})

        self.assertEqual((weather["lead"]["value"], weather["alert"]), ("83°F", "Heat Advisory"))
        self.assertEqual(weather["payload"]["status"], "serious")
        # The hour rain comes is the reading's instant on HQ's clock.
        self.assertLessEqual(
            {"Mostly Cloudy", "4\xa0PM\xa0· 60%", "71.5–86°"}, {m["value"] for m in weather["caption"]}
        )
        self.assertIn("Example Town, EX", text(html))
        # Each hour by its time, and a chance of rain said only when there is one.
        hours = {hour.attrs["title"]: hour.text for hour in with_class(html, "fact") if "title" in hour.attrs}
        self.assertIn("60%", hours["Showers"])
        self.assertIn("4\xa0PM", hours["Showers"])
        self.assertNotIn("%", hours["Cloudy"])

    def test_no_machine_draws_no_machine_reading(self):
        DashboardMachine.objects.all().delete()
        spec = panel_specs()[0]

        self.assertNotIn("infrastructure", [panel["id"] for panel in dashboard_panels()])
        # Even when it would be the only reading: Get started asks for a machine.
        with patch("hq.platform.application.glance.panel_specs", return_value=(spec,)):
            self.assertEqual(dashboard_panels(), ())

    def test_the_settings_page_holds_the_form_and_its_placeholder_is_not_a_coordinate(self):
        operator(self.client)
        response = self.client.get(reverse("dashboard_glance_settings"))

        (point,) = select(response, "name", "weather_point")
        self.assertNotRegex(point.attrs.get("placeholder", ""), r"\d")
        self.assertTrue(select(response, "name", "infrastructure_label"))

    def test_weather_waits_on_a_machine_a_controller_reaches(self):
        # The machine here is one no connection opens and no controller runs on.
        self.assertFalse(ProviderConnection.objects.exists())
        DashboardRefreshRequest.objects.create(panel_id="weather", requested_at=timezone.now())
        weather = next(p for p in dashboard_panels() if p["id"] == "weather")
        self.assertEqual((weather["refreshable"], weather["pending"]), (False, False))

    def test_each_panels_refresh_is_the_shared_ask_and_fetches_the_strip(self):
        panels = tuple({**panel, "ask": refresh_ask(panel["standing"])} for panel in dashboard_panels())
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": panels})

        controls = select(html, "data-ask-refresh", GLANCE_REGION)
        self.assertEqual(len(controls), len(panels))
        self.assertTrue(all("data-ask-outcome" in control.attrs for control in controls))
        self.assertEqual(len(select(html, "formaction", reverse("dashboard_glance"))), len(panels))
        self.assertTrue(select(html, "data-glance"))
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
        operator(self.client, "op")
        self.machine = declare("app-server", "machine", {"name": "app"})
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
            reported_machine("app-server", host=HOST),
            observed_at=timezone.now() - timedelta(hours=1),
        )

    def test_a_get_on_a_stale_glance_requests_nothing(self):
        with patch("hq.platform.application.cadence._ring") as doorbell:
            response = self.client.get(reverse("dashboard_glance"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-reading-age="stale"')
        self.assertFalse(DashboardRefreshRequest.objects.exists())
        doorbell.assert_not_called()

    def test_the_dashboard_page_requests_nothing(self):
        with patch("hq.platform.application.cadence._ring"):
            self.client.get(reverse("dashboard"))

        self.assertFalse(DashboardRefreshRequest.objects.exists())

    def test_the_dashboard_reads_its_glance_inside_its_projection(self):
        from .. import machines

        with patch.object(machines, "machine_catalog", wraps=machines.machine_catalog) as catalog:
            self.client.get(reverse("dashboard"))

        self.assertEqual(catalog.call_count, 1)

    def test_a_stale_post_requests_the_stale_panels_and_follows_them_unsaid(self):
        with doorbells() as rung:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("dashboard_glance"), form_keyed({"scope": "stale"}))

            self.assertIn("glance-doorbell", rung())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            list(DashboardRefreshRequest.objects.values_list("panel_id", flat=True)),
            [f"machine-{self.machine.pk}"],
        )
        # Nobody pressed for it: the region follows the reading, and the panel
        # keeps the one it has without saying a refresh is under way.
        self.assertTrue(select(response, "data-fragment-poll"))
        (part,) = select(response, "data-fragment-part", f"machine-{self.machine.pk}")
        self.assertNotIn(REFRESHING, part.text)

    def test_a_stale_post_asks_for_the_weather_never_read(self):
        # A point set and no forecast stored: the machine's reading is current.
        DashboardConfiguration.objects.create(weather_point="41.0000,-87.0000")
        readings.record(readings.machine_telemetry(self.machine.key), reported_machine("app-server", host=HOST))

        response = self.client.post(reverse("dashboard_glance"), form_keyed({"scope": "stale"}))

        self.assertEqual(list(DashboardRefreshRequest.objects.values_list("panel_id", flat=True)), ["weather"])
        # With no reading to keep, it shows its empty state.
        (part,) = select(response, "data-fragment-part", "weather")
        self.assertNotIn(REFRESHING, part.text)

    def test_a_stale_post_with_nothing_stale_requests_nothing(self):
        readings.record(readings.machine_telemetry(self.machine.key), reported_machine("app-server", host=HOST))
        response = self.client.post(reverse("dashboard_glance"), form_keyed({"scope": "stale"}))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DashboardRefreshRequest.objects.exists())
