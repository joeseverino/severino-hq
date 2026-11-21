from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import DashboardMachine, ProviderConnection, ProviderInventory
from hq.platform.application.markup_testing import hrefs, text, with_class
from hq.platform.core.testing import SignedInTestCase, declare, operator

from .. import cadence
from ..getting_started import remaining, steps
from ..projection import projection_scope


class GettingStartedTests(TestCase):
    def ids(self, found):
        return [step.id for step in found]

    def left(self):
        with projection_scope():
            return self.ids(remaining())

    def test_a_fresh_install_is_given_every_step_in_order(self):
        with projection_scope():
            self.assertEqual(self.ids(steps()), ["controller", "connection", "machine"])
        self.assertEqual(self.left(), ["controller", "connection", "machine"])

    def test_a_step_is_gone_once_hq_can_see_it_done(self):
        cadence.note_controller()
        ProviderConnection.objects.create(
            connection_ref="example-portainer",
            controller_id="example-controller",
            provider="portainer",
            endpoint="https://portainer.example.com",
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

        self.assertEqual(self.left(), ["machine"])

    def test_readings_without_a_local_heartbeat_mean_the_controller_ran(self):
        # A copy of another host's database: readings, and no heartbeat here.
        ProviderInventory.objects.create(kind="docker.container", observed_at=timezone.now(), records=[])

        self.assertNotIn("controller", self.left())

    def test_an_hq_with_everything_done_shows_no_steps(self):
        cadence.note_controller()
        ProviderConnection.objects.create(
            connection_ref="example-dns",
            controller_id="example-controller",
            provider="cloudflare_dns",
            endpoint="https://api.example.com",
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )
        machine = declare("app-host", "machine", {"name": "app-host"})
        DashboardMachine.objects.create(machine=machine, position=0)
        operator(self.client, "started")

        self.assertEqual(self.left(), [])
        self.assertNotContains(self.client.get("/"), "Get started")

    def test_the_dashboard_lists_what_is_left(self):
        operator(self.client, "new")

        response = self.client.get("/")

        self.assertContains(response, "Get started")
        self.assertContains(response, "Start the controller")


class GettingStartedRowTests(TestCase):
    def setUp(self):
        operator(self.client, "row")

    def test_a_done_step_stays_on_the_row_checked_while_others_are_left(self):
        cadence.note_controller()

        with override_settings(SEVERINO_HQ_SOURCE="https://github.com/example/hq"):
            response = self.client.get("/")

        self.assertEqual(len(with_class(response, "is-done")), 1)
        self.assertIn("2 left", text(response))
        self.assertIn("https://github.com/example/hq/blob/main/docs/Install.md", hrefs(response))


class ControllerTileTests(SignedInTestCase):
    """The dashboard's controller reading never says "not set up" over readings."""

    def test_with_nothing_read_it_is_not_set_up(self):
        self.assertContains(self.client.get("/"), "Not configured")

    def test_readings_without_a_heartbeat_here_count_as_its_arrival(self):
        # A copy of another host's database: readings, and no heartbeat here.
        ProviderInventory.objects.create(kind="docker.container", observed_at=timezone.now(), records=[])

        response = self.client.get("/")

        self.assertEqual(response.context["controller"].state, cadence.WorkerState.ONLINE)
        self.assertNotContains(response, "Not configured")
