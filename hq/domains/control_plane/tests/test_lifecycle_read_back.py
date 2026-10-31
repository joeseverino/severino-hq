"""Starting, stopping or restarting a container reads it back, and a stop that
cuts HQ's own way to a machine says so before it is pressed."""

import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from hq.domains.control_plane.models import ManagedResource, OperationRequest, ProviderInventory, ReadRequest
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.platform.application.adoption_testing import connection, managing_everything
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.controller import ControllerReport, report_operation
from hq.platform.application.resource_operations import ACTION_CAPABILITY
from hq.platform.application.security import Capability, Principal

DIRECTORY = Path(tempfile.mkdtemp())
(DIRECTORY / "heartbeat").touch()
# A controller that arrived a moment ago, and a doorbell nothing watches.
MARKERS = override_settings(
    SEVERINO_CONTROLLER_DOORBELL=str(DIRECTORY / "doorbell"),
    SEVERINO_ACTIVITY_MARKER=str(DIRECTORY / "activity"),
    SEVERINO_CONTROLLER_HEARTBEAT=str(DIRECTORY / "heartbeat"),
)
LIFECYCLE = (OperationRequest.Action.RESTART, OperationRequest.Action.START, OperationRequest.Action.STOP)
HOST = "example-host"


def containers(*states: tuple[str, str, int]) -> None:
    """The container sweep, as the Portainer reader stores it: each one's name, state and published port."""

    row, _created = ProviderInventory.objects.update_or_create(
        kind=CONTAINER_KIND,
        defaults={
            "records": [
                {"name": name, "host": HOST, "state": state, "ports": [port], "image": "example/app:1"}
                for name, state, port in states
            ],
            "reachable": True,
            "observed_at": timezone.now(),
        },
    )
    # Read before anything in the test asked for it.
    ProviderInventory.objects.filter(pk=row.pk).update(updated_at=timezone.now() - timedelta(minutes=1))


def declare(name: str) -> ManagedResource:
    return ManagedResource.objects.create(
        key=name, kind=CONTAINER_KIND, spec={"connection_ref": "example-portainer", "host": HOST, "name": name}
    )


def asked() -> set[str]:
    return set(ReadRequest.objects.values_list("kind", flat=True))


class Estate(TestCase):
    def setUp(self):
        managing_everything()
        ManagedResource.objects.create(
            key=HOST, kind="machine", spec={"name": HOST, "role": "Docker host", "addresses": ["192.0.2.10"]}
        )
        self.client.force_login(get_user_model().objects.create_user(username="operator", password="x" * 20))


@MARKERS
class ReadBackTests(Estate):
    def setUp(self):
        super().setUp()
        containers(("example-web", "running", 8080))
        self.web = declare("example-web")

    def press(self, action):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse(f"control_plane:{action}", kwargs={"key": "example-web"}))

    def test_each_verb_asks_for_the_containers_to_be_read_once_it_is_queued(self):
        for action in LIFECYCLE:
            with self.subTest(action=action):
                ReadRequest.objects.all().delete()

                self.press(action)

                self.assertTrue(OperationRequest.objects.filter(action=action).exists())
                self.assertEqual(asked(), {CONTAINER_KIND})

    def test_a_refused_request_asks_for_nothing(self):
        self.web.enabled = False
        self.web.save(update_fields=["enabled"])

        self.press(OperationRequest.Action.STOP)

        self.assertFalse(OperationRequest.objects.exists())
        self.assertEqual(asked(), set())

    def test_a_caller_without_the_grant_asks_for_nothing(self):
        reader = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        with self.captureOnCommitCallbacks(execute=True):
            result = execute_capability(ACTION_CAPABILITY["stop"], {}, principal=reader, target="example-web")

        self.assertEqual(result["error"]["code"], "forbidden")
        self.assertEqual(asked(), set())

    def test_the_page_follows_the_read_and_then_offers_start(self):
        page = reverse("control_plane:detail", kwargs={"key": "example-web"})
        start = reverse("control_plane:start", kwargs={"key": "example-web"})
        stop = reverse("control_plane:stop", kwargs={"key": "example-web"})
        self.assertNotContains(self.client.get(page), f'action="{start}"')

        self.press(OperationRequest.Action.STOP)
        drawn = self.client.get(page)
        # The page names itself to the visit ask, and the ask follows the read
        # the stop asked for.
        self.assertContains(drawn, 'name="subject" value="resource"')
        following = self.client.post(reverse("control_plane:visit"), {"subject": "resource", "name": "example-web"})
        self.assertEqual(following.status_code, 202)
        self.assertTrue(following.json()["status"])

        containers(("example-web", "exited", 8080))
        ProviderInventory.objects.filter(kind=CONTAINER_KIND).update(updated_at=timezone.now())

        landed = self.client.post(reverse("control_plane:visit"), {"subject": "resource", "name": "example-web"})
        self.assertFalse(landed.json()["live"])
        redrawn = self.client.get(page)
        self.assertContains(redrawn, f'action="{start}"')
        self.assertNotContains(redrawn, stop)


class SelfDependencyTests(Estate):
    def setUp(self):
        super().setUp()
        containers(("example-portainer", "running", 9000), ("example-web", "running", 8080))
        reader = connection("portainer", "example-portainer")
        reader.endpoint = "http://192.0.2.10:9000"
        reader.reaches = [HOST, "example-edge"]
        reader.save(update_fields=["endpoint", "reaches"])
        self.portainer = declare("example-portainer")
        declare("example-web")

    def confirm(self, key):
        return self.client.get(reverse("control_plane:stop", kwargs={"key": key}))

    def test_stopping_the_container_a_connection_runs_in_says_what_stops_and_how_to_start_it(self):
        response = self.confirm("example-portainer")

        self.assertContains(response, 'data-cuts-connection="example-portainer"')
        said = strip_tags(response.content.decode())
        self.assertIn("cannot start this container again", said)
        self.assertIn("docker start example-portainer", said)
        self.assertIn("example-edge", said)

    def test_an_ordinary_container_carries_no_warning(self):
        self.assertNotContains(self.confirm("example-web"), "data-cuts-connection")

    def test_a_container_acting_through_another_connection_can_still_be_started(self):
        from hq.platform.application.resource_capabilities import channels_inside

        self.portainer.spec = {**self.portainer.spec, "connection_ref": "example-other-portainer"}

        channels = channels_inside(self.portainer)

        self.assertEqual(len(channels), 1)
        channel = channels[0]
        self.assertEqual(channel.connection_ref, "example-portainer")
        self.assertFalse(channel.carries_its_verbs)


class LoopbackSelfDependencyTests(Estate):
    """A connection a controller reaches on loopback is on that controller's
    machine, even when another machine publishes the same port."""

    def setUp(self):
        super().setUp()
        ManagedResource.objects.create(
            key="example-edge", kind="machine", spec={"name": "example-edge", "addresses": ["192.0.2.20"]}
        )
        ProviderInventory.objects.create(
            kind=CONTAINER_KIND,
            records=[
                {"name": "example-portainer", "host": HOST, "state": "running", "ports": [9000]},
                {"name": "example-agent", "host": "example-edge", "state": "running", "ports": [9000]},
            ],
            observed_at=timezone.now(),
        )
        reader = connection("portainer", "example-portainer")
        reader.endpoint = "http://127.0.0.1:9000"
        reader.controller_id = HOST
        reader.reaches = [HOST, "example-edge"]
        reader.save(update_fields=["endpoint", "controller_id", "reaches"])
        declare("example-portainer")
        ManagedResource.objects.create(
            key="example-agent",
            kind=CONTAINER_KIND,
            spec={"connection_ref": "example-portainer", "host": "example-edge", "name": "example-agent"},
        )

    def test_stopping_the_container_on_the_controllers_machine_warns(self):
        response = self.client.get(reverse("control_plane:stop", kwargs={"key": "example-portainer"}))

        self.assertContains(response, 'data-cuts-connection="example-portainer"')

    def test_the_same_port_on_another_machine_carries_no_warning(self):
        response = self.client.get(reverse("control_plane:stop", kwargs={"key": "example-agent"}))

        self.assertNotContains(response, "data-cuts-connection")


@MARKERS
class ReportReadBackTests(Estate):
    """A sweep already running when an operation was queued may answer its ask
    with what it read before the operation acted, so the report asks again."""

    def setUp(self):
        super().setUp()
        containers(("example-web", "running", 8080))
        self.web = declare("example-web")

    def report(self, action, *, success=True):
        operation = OperationRequest.objects.create(
            resource=self.web,
            action=action,
            state=OperationRequest.State.CLAIMED,
            requested_actor="operator",
            requested_interface="web",
            idempotency_key=f"report-{action}-{success}",
            input={"generation": self.web.generation},
            claimed_by="example-controller",
            claimed_at=timezone.now(),
            lease_expires_at=timezone.now() + timedelta(minutes=5),
        )
        # The ask made when it was queued, already answered by the early sweep.
        ReadRequest.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            report_operation(
                str(operation.id),
                ControllerReport(success=success, observed_generation=self.web.generation, status={}),
                controller_id="example-controller",
            )

    def test_a_reported_lifecycle_operation_asks_for_its_kind_again(self):
        for action in LIFECYCLE:
            for success in (True, False):
                with self.subTest(action=action, success=success):
                    self.report(action, success=success)

                    self.assertEqual(asked(), {CONTAINER_KIND})

    def test_a_reconcile_asks_for_nothing(self):
        self.report(OperationRequest.Action.RECONCILE)

        self.assertEqual(asked(), set())
