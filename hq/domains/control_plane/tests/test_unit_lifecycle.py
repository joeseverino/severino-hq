"""A container whose label names the systemd unit that runs it is stopped,
started and restarted through that unit, which HQ reaches only on the machine
HQ runs on: there the verbs name the unit, elsewhere they say why not."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from hq.domains.control_plane.models import ManagedResource, OperationRequest, ProviderInventory
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.platform.application.adoption_testing import connection, managing_everything
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.resource_capabilities import LIFECYCLE_VERBS, resource_capabilities
from hq.platform.application.resource_operations import ACTION_CAPABILITY
from hq.platform.application.security import Capability, Principal

HQ_MACHINE, HQ_ADDRESS = "example-host", "192.0.2.10"
OTHER_MACHINE = "example-edge"
SUPERVISED_HERE, SUPERVISED_THERE, UNSUPERVISED = "example-hermes", "example-agent", "example-web"


def runs_hq_at(address):
    """HQ's own host has this address, so the machine holding it runs HQ."""

    return mock.patch(
        "hq.platform.application.hq_self.host_addresses",
        return_value=frozenset({"127.0.0.1", "::1", address}),
    )


@override_settings(SEVERINO_SITE_HOST="hq.example.com", ALLOWED_HOSTS=["hq.example.com", "testserver"])
class UnitLifecycleTests(TestCase):
    def setUp(self):
        managing_everything()
        for name, address in ((HQ_MACHINE, HQ_ADDRESS), (OTHER_MACHINE, "192.0.2.20")):
            ManagedResource.objects.create(key=name, kind="machine", spec={"name": name, "addresses": [address]})
        ProviderInventory.objects.create(
            kind=CONTAINER_KIND,
            records=[
                {
                    "name": SUPERVISED_HERE,
                    "host": HQ_MACHINE,
                    "state": "running",
                    "ports": [9000],
                    "unit": "example-hermes.service",
                },
                {
                    "name": SUPERVISED_THERE,
                    "host": OTHER_MACHINE,
                    "state": "running",
                    "ports": [],
                    "unit": "example-agent.service",
                },
                {"name": UNSUPERVISED, "host": OTHER_MACHINE, "state": "running", "ports": [8080]},
            ],
            reachable=True,
            observed_at=timezone.now(),
        )
        for name, host in (
            (SUPERVISED_HERE, HQ_MACHINE),
            (SUPERVISED_THERE, OTHER_MACHINE),
            (UNSUPERVISED, OTHER_MACHINE),
        ):
            ManagedResource.objects.create(
                key=name, kind=CONTAINER_KIND, spec={"connection_ref": "example-portainer", "host": host, "name": name}
            )
        self.client.force_login(get_user_model().objects.create_user(username="operator", password="x" * 20))
        placed = runs_hq_at(HQ_ADDRESS)
        placed.start()
        self.addCleanup(placed.stop)

    def offered(self, key):
        return resource_capabilities(ManagedResource.objects.get(key=key)).actions

    def press(self, key, verb):
        return self.client.post(reverse(f"control_plane:{verb}", kwargs={"key": key}))

    def test_on_hqs_machine_a_stop_names_the_unit_and_is_offered(self):
        stop = self.offered(SUPERVISED_HERE)["stop"]
        detail = self.client.get(reverse("control_plane:detail", kwargs={"key": SUPERVISED_HERE}))
        confirm = self.client.get(reverse("control_plane:stop", kwargs={"key": SUPERVISED_HERE}))

        self.assertTrue(stop.enabled, stop.reason)
        self.assertContains(detail, 'data-run-by-unit="example-hermes.service"')
        self.assertContains(confirm, 'data-run-by-unit="example-hermes.service"')
        self.press(SUPERVISED_HERE, "stop")
        self.assertTrue(OperationRequest.objects.filter(resource__key=SUPERVISED_HERE, action="stop").exists())

    def test_elsewhere_each_verb_says_why_and_is_refused(self):
        detail = self.client.get(reverse("control_plane:detail", kwargs={"key": SUPERVISED_THERE}))
        offered = {verb: allowed for verb, allowed in self.offered(SUPERVISED_THERE).items() if verb in LIFECYCLE_VERBS}
        reasons = {allowed.reason for allowed in offered.values()}

        self.assertTrue(offered)
        self.assertFalse(any(allowed.enabled for allowed in offered.values()))
        (reason,) = reasons
        self.assertTrue(reason)
        self.assertIn(reason, detail.content.decode())
        self.press(SUPERVISED_THERE, "stop")
        operator = Principal("example-agent", "mcp", frozenset(Capability))
        result = execute_capability(ACTION_CAPABILITY["restart"], {}, principal=operator, target=SUPERVISED_THERE)
        # The refusal says what the page said.
        self.assertEqual(result["error"]["message"], reason)
        self.assertFalse(OperationRequest.objects.filter(resource__key=SUPERVISED_THERE).exists())

    def test_a_container_no_unit_runs_is_offered_anywhere(self):
        self.assertTrue(self.offered(UNSUPERVISED)["stop"].enabled)

    def test_a_connection_inside_it_is_started_again_through_its_unit(self):
        reader = connection("portainer", "example-portainer")
        reader.endpoint = f"http://{HQ_ADDRESS}:9000"
        reader.reaches = [HQ_MACHINE, OTHER_MACHINE]
        reader.save(update_fields=["endpoint", "reaches"])

        said = strip_tags(
            self.client.get(reverse("control_plane:stop", kwargs={"key": SUPERVISED_HERE})).content.decode()
        )

        self.assertIn("sudo systemctl start example-hermes.service", said)
        self.assertNotIn("docker start", said)
