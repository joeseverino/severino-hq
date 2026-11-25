"""The dashboard's settings and refresh, written through their capabilities."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
)
from hq.platform.application import readings
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.glance import DASHBOARD_SETTINGS
from hq.platform.application.glance_refresh import DASHBOARD_REFRESH, DASHBOARD_REFRESH_STALE
from hq.platform.application.markup_testing import text
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.security import OPERATOR_CAPABILITIES, Capability, Principal, cli_principal
from hq.platform.application.tests.glance_readings import HOST
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import connection, declare, operator

GRANTED = frozenset(Capability)
CREDENTIALS = (
    Principal("example-agent", "mcp", GRANTED, granted=GRANTED),
    Principal("example-client", "api", GRANTED, granted=GRANTED),
)


def denials(capability: str) -> int:
    return AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=capability).count()


def without(username: str, capability: Capability):
    """The view's operator, lacking ``capability``."""

    return patch(
        "hq.platform.application.web_writes.web_principal",
        return_value=Principal(username, "web", OPERATOR_CAPABILITIES - {capability}),
    )


class DashboardSettingsTests(TestCase):
    def setUp(self):
        operator(self.client, "op")
        self.url = reverse("dashboard_glance_settings")

    def save(self, **fields):
        return self.client.post(
            self.url, form_keyed({"weather_label": "Outside", "infrastructure_label": "Lab", **fields}), follow=True
        )

    def test_saved_settings_are_kept_and_recorded_under_the_operator(self):
        response = self.save(weather_point=" 41.1, -87.2 ")

        self.assertRedirects(response, reverse("dashboard"))
        self.assertIn("Dashboard settings saved.", text(response))
        configuration = DashboardConfiguration.objects.get()
        self.assertEqual(
            (configuration.weather_point, configuration.weather_label, configuration.infrastructure_label),
            ("41.1000,-87.2000", "Outside", "Lab"),
        )
        saved = AuditLog.objects.get(object_type="Dashboard", object_id=str(configuration.pk))
        self.assertEqual((saved.metadata["actor"], saved.metadata["operation"]), ("op", DASHBOARD_SETTINGS))

    def test_a_location_that_is_not_one_is_refused_on_the_page(self):
        for point, said in (
            ("north", "weather_point[0] must be a valid number; weather_point[1] is required."),
            ("a, b", "weather_point[0] must be a valid number; weather_point[1] must be a valid number."),
            ("91, 0", "weather_point[0] must be at most 90."),
        ):
            with self.subTest(point=point):
                self.assertIn(said, text(self.save(weather_point=point)))
        self.assertFalse(DashboardConfiguration.objects.filter(weather_label="Outside").exists())

    def test_an_operator_without_the_grant_is_refused_on_the_page(self):
        with without("op", Capability.ARRANGE_DASHBOARD):
            response = self.save(weather_point="41.1, -87.2")

        self.assertIn("arrange_dashboard", text(response))
        self.assertFalse(DashboardConfiguration.objects.filter(weather_label="Outside").exists())
        self.assertEqual(denials(DASHBOARD_SETTINGS), 1)

    def test_only_a_person_in_the_browser_changes_them(self):
        for principal in (*CREDENTIALS, cli_principal()):
            with self.subTest(interface=principal.interface):
                result = execute_capability(DASHBOARD_SETTINGS, {"weather_label": "Taken"}, principal=principal)

                self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(DashboardConfiguration.objects.filter(weather_label="Taken").exists())
        self.assertEqual(denials(DASHBOARD_SETTINGS), 3)


class DashboardRefreshTests(TestCase):
    def setUp(self):
        operator(self.client, "op")
        machine = declare("app-server", "machine", {"name": "app"})
        connection("ssh", "app-ssh", controller_id="app-server", endpoint="100.64.0.10", reaches=["app"])
        DashboardMachine.objects.create(machine=machine)
        readings.record(
            readings.machine_telemetry(machine.key),
            {"key": machine.key, "host": HOST},
            observed_at=timezone.now() - timedelta(hours=1),
        )
        self.panel = f"machine-{machine.pk}"

    def refresh(self, **fields):
        with patch("hq.platform.application.cadence._ring"):
            return self.client.post(
                reverse("dashboard_glance"), form_keyed(fields), headers={"X-Requested-With": "XMLHttpRequest"}
            )

    def test_each_refresh_asks_for_the_machine_and_is_recorded(self):
        for name, fields in ((DASHBOARD_REFRESH, {}), (DASHBOARD_REFRESH_STALE, {"scope": "stale"})):
            with self.subTest(name=name):
                DashboardRefreshRequest.objects.all().delete()

                response = self.refresh(**fields)

                self.assertEqual(response.status_code, 202)
                self.assertEqual(list(DashboardRefreshRequest.objects.values_list("panel_id", flat=True)), [self.panel])
                asked = AuditLog.objects.filter(object_type="Read request", metadata__operation=name).get()
                request = DashboardRefreshRequest.objects.get(panel_id=self.panel)
                self.assertEqual((asked.metadata["actor"], asked.object_id), ("op", str(request.pk)))

    def test_an_operator_without_the_grant_is_refused_and_asks_for_nothing(self):
        for name, fields in ((DASHBOARD_REFRESH, {}), (DASHBOARD_REFRESH_STALE, {"scope": "stale"})):
            with self.subTest(name=name), without("op", Capability.MANAGE_INFRASTRUCTURE):
                self.refresh(**fields)

                self.assertEqual(denials(name), 1)
        self.assertFalse(DashboardRefreshRequest.objects.exists())

    def test_a_credential_without_infrastructure_is_refused(self):
        reader = Principal("example-agent", "mcp", frozenset({Capability.READ}))
        for name in (DASHBOARD_REFRESH, DASHBOARD_REFRESH_STALE):
            with self.subTest(name=name):
                self.assertEqual(execute_capability(name, {}, principal=reader)["error"]["code"], "forbidden")
                self.assertEqual(denials(name), 1)
        self.assertFalse(DashboardRefreshRequest.objects.exists())

    def test_a_refresh_names_no_panel(self):
        for name in (DASHBOARD_REFRESH, DASHBOARD_REFRESH_STALE):
            with self.subTest(name=name):
                result = execute_capability(name, {"panels": ["weather"]}, principal=cli_principal())

                self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(DashboardRefreshRequest.objects.exists())
