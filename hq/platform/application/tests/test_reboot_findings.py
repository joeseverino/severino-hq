"""The restart reading, and what HQ says when a machine waits on a restart."""

from datetime import UTC, timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.observations import OBSERVATIONS

from ..findings import estate_findings
from ..inventory_testing import store
from ..projection import projection_scope
from .test_tailnet_posture import EVERYTHING, raised, tailnet_connection

# Written out rather than imported, so the module loads where the reading is
# not declared yet.
KIND = "host.reboot"
EDGE_KIND = "host.edge_reboot"
RULE = "reboot-pending"


def stamp(age: timedelta = timedelta(0)) -> str:
    """An instant ``age`` ago (ahead, when negative), as the launcher writes one."""

    return (timezone.now() - age).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def reading(*, pending: bool = True, **said) -> dict:
    """The machine's restart state as the controller reports it: by default a
    kernel and a library installed two days ago, nothing scheduled."""

    waiting = {"since": stamp(timedelta(days=2)), "packages": ["linux-image-generic", "libc6"]} if pending else {}
    return {"pending": pending, **waiting, "read_at": stamp(), **said}


def sweep(*records, kind: str = KIND, **fields) -> None:
    """The reading as a sweep stores it, through the schema."""

    kept, refused = OBSERVATIONS[kind].clean(records)
    assert not refused, records
    store(kind, *kept, **fields)


def edge(ref: str = "example-edge") -> None:
    """An SSH transport, which is the machine at its other end, named by its ref."""

    from hq.domains.control_plane.models import ProviderConnection

    ProviderConnection.objects.create(
        connection_ref=ref,
        controller_id="example-controller",
        provider="ssh",
        endpoint="198.51.100.7:22",
        reachable=True,
        observed_at=timezone.now(),
    )


def evidence(finding) -> dict[str, str]:
    return {item["label"]: item["value"] for item in finding["evidence"]}


def steps(rule: str = RULE) -> list[str]:
    with projection_scope():
        (finding,) = [found for found in estate_findings(principal=EVERYTHING) if found.rule == rule]
    return [step.command for step in finding.steps]


class WaitingTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_the_machine_the_controller_runs_on_shows_it_on_its_page(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from hq.domains.control_plane.models import ManagedResource

        # A controller reports as its machine's name, as it does by default.
        ManagedResource.objects.create(key="example-controller", kind="machine", spec={"name": "example-controller"})
        sweep(reading())
        self.client.force_login(get_user_model().objects.create_user(username="an-operator", password="not-used"))

        page = self.client.get(reverse("control_plane:machine", kwargs={"name": "example-controller"}))

        # Its "check again" returns to the rule's findings, carried URL-encoded.
        self.assertContains(page, f"rule%3D{RULE}")

    def test_an_edge_that_waits_shows_it_on_its_own_machines_page(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        edge()
        # The controller's own machine waits on nothing; the edge's receiver
        # says it waits on its kernel.
        sweep(reading(pending=False))
        sweep(reading(connection_ref="example-edge", packages=["linux-image-example"]), kind=EDGE_KIND)
        self.client.force_login(get_user_model().objects.create_user(username="an-operator", password="not-used"))

        (finding,) = raised(RULE)
        page = self.client.get(reverse("control_plane:machine", kwargs={"name": "example-edge"}))

        self.assertIn("example-edge", finding["title"])
        self.assertIn("linux-image-example", evidence(finding).get("Packages", ""))
        self.assertContains(page, f"rule%3D{RULE}")
        self.assertContains(page, "linux-image-example")
        # Its steps run on the edge, the machine that waits.
        commands = steps()
        self.assertEqual(len(commands), 2)
        self.assertTrue(all("example-edge" in command for command in commands), commands)
        self.assertIn("/run/reboot-required.pkgs", commands[0])
        self.assertIn("shutdown -r", commands[1])
