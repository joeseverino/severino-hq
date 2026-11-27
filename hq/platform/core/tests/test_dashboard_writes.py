"""The dashboard's settings and refresh, written through their capabilities."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import DashboardConfiguration, DashboardMachine, DashboardRefreshRequest
from hq.platform.application.glance_refresh import DASHBOARD_REFRESH, DASHBOARD_REFRESH_STALE
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.security import OPERATOR_CAPABILITIES, Capability, Principal
from hq.platform.application.tests.glance_readings import HOST, keep_machine
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import connection, declare, operator


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

    def test_a_location_that_is_not_one_is_refused_on_the_page(self):
        for point in ("north", "a, b", "91, 0"):
            with self.subTest(point=point):
                self.save(weather_point=point)

                self.assertFalse(DashboardConfiguration.objects.filter(weather_label="Outside").exists())


class DashboardRefreshTests(TestCase):
    def setUp(self):
        operator(self.client, "op")
        machine = declare("app-server", "machine", {"name": "app"})
        connection("ssh", "app-ssh", controller_id="app-server", endpoint="100.64.0.10", reaches=["app"])
        DashboardMachine.objects.create(machine=machine)
        keep_machine({"key": machine.key, "host": HOST}, at=timezone.now() - timedelta(hours=1))

    def refresh(self, **fields):
        with patch("hq.platform.application.cadence._ring"):
            return self.client.post(
                reverse("dashboard_glance"), form_keyed(fields), headers={"X-Requested-With": "XMLHttpRequest"}
            )

    def test_an_operator_without_the_grant_is_refused_and_asks_for_nothing(self):
        for name, fields in ((DASHBOARD_REFRESH, {}), (DASHBOARD_REFRESH_STALE, {"scope": "stale"})):
            with self.subTest(name=name), without("op", Capability.MANAGE_INFRASTRUCTURE):
                self.refresh(**fields)

                self.assertEqual(denials(name), 1)
        self.assertFalse(DashboardRefreshRequest.objects.exists())
