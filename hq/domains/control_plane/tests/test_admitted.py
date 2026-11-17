"""A controller's report is written only with the bridge's admission."""

from types import SimpleNamespace

from django.test import TestCase

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory
from hq.platform.application.analytics import record_analytics
from hq.platform.application.glance import record_dashboard_observations
from hq.platform.application.inventory import record_connections, record_step_failures
from hq.platform.application.security import cli_principal, internal_principal
from hq.platform.application.sweep import record_sweep

A_SWEEP = {"adguard.rewrite": {"ok": True, "records": []}}
# Every writer the bridge hands a controller's report, with a report it takes.
WRITERS = (
    (record_sweep, A_SWEEP),
    (record_connections, []),
    (record_step_failures, []),
    (record_analytics, {"sites": []}),
    (record_dashboard_observations, []),
)


class AdmittedTests(TestCase):
    def assert_refused(self, admitted):
        for writer, report in WRITERS:
            with self.subTest(writer=writer.__name__), self.assertRaises(TypeError):
                writer(report, admitted=admitted)
        self.assertFalse(ProviderInventory.objects.exists())
        self.assertFalse(ProviderConnection.objects.exists())

    def test_a_writer_handed_a_principal_writes_nothing(self):
        for writer, report in WRITERS:
            with self.subTest(writer=writer.__name__), self.assertRaises(TypeError):
                writer(report, principal=cli_principal(), controller_id="example-controller")
        self.assertFalse(ProviderInventory.objects.exists())

    def test_a_stand_in_for_an_admission_writes_nothing(self):
        self.assert_refused(SimpleNamespace(controller_id="example-controller", principal=internal_principal("x")))

    def test_a_bare_admission_writes_nothing(self):
        from hq.domains.control_plane.bridge_actions import Admitted

        self.assert_refused(object.__new__(Admitted))

    def test_an_admission_sealed_elsewhere_writes_nothing(self):
        from hq.domains.control_plane.bridge_actions import Admission, Admitted
        from hq.platform.application.sealing import sealer

        seal, _unseal = sealer(Admitted, Admission)
        self.assert_refused(seal(Admission("example-controller", internal_principal("x"))))

    def test_nothing_but_the_bridge_makes_an_admission(self):
        from hq.domains.control_plane import bridge_actions

        with self.assertRaises(TypeError):
            bridge_actions.Admitted("example-controller")  # type: ignore[call-arg]
        with self.assertRaises(ImportError):
            from hq.domains.control_plane.bridge_actions import _ADMITTER  # noqa: F401  # the key it must not hold

    def test_a_report_through_the_bridge_is_stored_against_its_controller(self):
        from hq.platform.application.report_testing import bridged

        bridged("inventory", A_SWEEP, controller_id="example-controller")

        self.assertEqual(ProviderInventory.objects.get(kind="adguard.rewrite").controller_id, "example-controller")
