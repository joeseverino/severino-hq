"""A controller's report is written only with the bridge's admission."""

from django.test import TestCase

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory
from hq.platform.application.analytics import record_analytics
from hq.platform.application.glance import record_dashboard_observations
from hq.platform.application.inventory import record_connections, record_step_failures
from hq.platform.application.security import cli_principal
from hq.platform.application.sweep import record_sweep

# Every writer the bridge hands a controller's report.
WRITERS = (record_sweep, record_connections, record_step_failures, record_analytics, record_dashboard_observations)
A_SWEEP = {"adguard.rewrite": {"ok": True, "records": []}}


class AdmittedTests(TestCase):
    def test_a_writer_handed_a_principal_writes_nothing(self):
        for writer in WRITERS:
            with self.subTest(writer=writer.__name__), self.assertRaises(TypeError):
                writer(A_SWEEP, principal=cli_principal(), controller_id="example-controller")
        self.assertFalse(ProviderInventory.objects.exists())
        self.assertFalse(ProviderConnection.objects.exists())

    def test_nothing_but_the_bridge_makes_an_admission(self):
        from hq.domains.control_plane.bridge_actions import Admitted

        with self.assertRaises(TypeError):
            Admitted("example-controller")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            Admitted("example-controller", admitter=object())

    def test_a_report_through_the_bridge_is_stored_against_its_controller(self):
        from hq.platform.application.report_testing import bridged

        bridged("inventory", A_SWEEP, controller_id="example-controller")

        self.assertEqual(ProviderInventory.objects.get(kind="adguard.rewrite").controller_id, "example-controller")
