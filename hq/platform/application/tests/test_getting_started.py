import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import DashboardMachine, ManagedResource, ProviderConnection, ProviderInventory

from .. import cadence
from ..getting_started import remaining, steps
from ..projection import projection_scope
from .markup import hrefs, text, with_class


class GettingStartedTests(TestCase):
    def setUp(self):
        # A heartbeat file per test, so one test's controller does not count for another.
        heartbeat = Path(tempfile.mkdtemp()) / "controller-heartbeat"
        self.enterContext(override_settings(SEVERINO_CONTROLLER_HEARTBEAT=str(heartbeat)))

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
        machine = ManagedResource.objects.create(key="app-host", kind="machine", spec={"name": "app-host"})
        DashboardMachine.objects.create(machine=machine, position=0)
        self.client.force_login(get_user_model().objects.create_user("started"))

        self.assertEqual(self.left(), [])
        self.assertNotContains(self.client.get("/"), "Get started")

    def test_the_dashboard_lists_what_is_left(self):
        self.client.force_login(get_user_model().objects.create_user("new"))

        response = self.client.get("/")

        self.assertContains(response, "Get started")
        self.assertContains(response, "Run the controller")


class GettingStartedRowTests(TestCase):
    def setUp(self):
        heartbeat = Path(tempfile.mkdtemp()) / "controller-heartbeat"
        self.enterContext(override_settings(SEVERINO_CONTROLLER_HEARTBEAT=str(heartbeat)))
        self.client.force_login(get_user_model().objects.create_user("row"))

    def test_a_done_step_stays_on_the_row_checked_while_others_are_left(self):
        cadence.note_controller()

        with override_settings(SEVERINO_HQ_SOURCE="https://github.com/example/hq"):
            response = self.client.get("/")

        self.assertEqual(len(with_class(response, "is-done")), 1)
        self.assertIn("2 left", text(response))
        self.assertIn("https://github.com/example/hq/blob/main/docs/Install.md", hrefs(response))


class ControllerTileTests(TestCase):
    """The dashboard's controller reading never says "not set up" over readings."""

    def setUp(self):
        heartbeat = Path(tempfile.mkdtemp()) / "controller-heartbeat"
        self.enterContext(override_settings(SEVERINO_CONTROLLER_HEARTBEAT=str(heartbeat)))
        self.client.force_login(get_user_model().objects.create_superuser("tile-reader"))

    def test_with_nothing_read_it_is_not_set_up(self):
        self.assertContains(self.client.get("/"), "Not set up yet")

    def test_readings_without_a_heartbeat_here_count_as_its_arrival(self):
        # A copy of another host's database: readings, and no heartbeat here.
        ProviderInventory.objects.create(kind="docker.container", observed_at=timezone.now(), records=[])

        response = self.client.get("/")

        self.assertContains(response, "dash-controller is-arriving")
        self.assertNotContains(response, "Not set up yet")
