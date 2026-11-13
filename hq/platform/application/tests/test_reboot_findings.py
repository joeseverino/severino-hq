"""The restart reading, and what HQ says when a machine waits on a restart."""

from datetime import UTC, timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.domains.control_plane.bridge_contract import contract
from hq.domains.control_plane.observations import OBSERVATIONS

from ..cadence import slowest_sweep_interval
from ..derivation_testing import frozen, uncached
from ..findings import derive_findings
from ..inventory_testing import store
from ..moments import when
from ..projection import projection_scope
from ..timestamps import moment
from ..topology import derive_topology
from .test_tailnet_posture import EVERYTHING, raised, tailnet_connection

# Written out rather than imported, so the module loads where the reading is
# not declared yet.
KIND = "host.reboot"
RULE = "reboot-pending"


def stamp(age: timedelta = timedelta(0)) -> str:
    """An instant ``age`` ago (ahead, when negative), as the launcher writes one."""

    return (timezone.now() - age).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def reading(*, pending: bool = True, **said) -> dict:
    """The machine's restart state as the controller reports it: by default a
    kernel and a library installed two days ago, nothing scheduled."""

    waiting = {"since": stamp(timedelta(days=2)), "packages": ["linux-image-generic", "libc6"]} if pending else {}
    return {"pending": pending, **waiting, "read_at": stamp(), **said}


def sweep(*records, **fields) -> None:
    """The reading as a sweep stores it, through the schema."""

    kept, refused = OBSERVATIONS[KIND].clean(records)
    assert not refused, records
    store(KIND, *kept, **fields)


def evidence(finding) -> dict[str, str]:
    return {item["label"]: item["value"] for item in finding["evidence"]}


def steps(rule: str = RULE) -> list[str]:
    with projection_scope():
        (finding,) = [
            found
            for found in derive_findings(derive_topology(principal=EVERYTHING), principal=EVERYTHING)
            if found.rule == rule
        ]
    return [step.command for step in finding.steps]


class RecordTests(SimpleTestCase):
    def test_the_record_is_the_contracts(self):
        from hq.domains.control_plane.observations.host import HostRebootRecord

        declared = contract()["components"]["schemas"]["HostRebootRecord"]

        self.assertEqual(set(HostRebootRecord.model_fields), set(declared["properties"]))

    def test_a_path_or_a_device_is_never_kept(self):
        (kept,), refused = OBSERVATIONS[KIND].clean([reading(path="/run/reboot-required", device="/dev/sda2")])

        self.assertEqual(refused, 0)
        self.assertNotIn("path", kept)
        self.assertNotIn("device", kept)

    def test_a_package_dpkg_would_not_name_is_refused(self):
        _kept, refused = OBSERVATIONS[KIND].clean(
            [reading(packages=["linux image"]), reading(packages=["a|b"]), reading(packages=["Libc6"])]
        )

        self.assertEqual(refused, 3)


class WaitingTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_a_waiting_restart_says_since_when_and_for_what(self):
        record = reading()
        sweep(record)

        (finding,) = raised(RULE)

        self.assertEqual(finding["severity"], "attention")
        said = evidence(finding)
        self.assertEqual(said.get("Waiting since"), when(moment(record["since"])))
        self.assertIn("linux-image-generic", said.get("Packages", ""))
        self.assertIn("libc6", said.get("Packages", ""))
        self.assertNotIn("Scheduled", said)

    def test_its_steps_read_the_packages_then_schedule_the_restart(self):
        sweep(reading())

        commands = steps()

        self.assertEqual(len(commands), 2)
        self.assertIn("/run/reboot-required.pkgs", commands[0])
        self.assertIn("shutdown -r", commands[1])

    def test_a_scheduled_restart_says_when(self):
        at = stamp(-timedelta(hours=5))
        sweep(reading(scheduled_action="reboot", scheduled_at=at))

        (finding,) = raised(RULE)

        self.assertIn(when(moment(at)), evidence(finding).get("Scheduled", ""))
        self.assertIn(when(moment(at)), finding["title"])
        self.assertTrue(any("shutdown --show" in command for command in steps()))
        self.assertFalse(any("shutdown -r" in command for command in steps()))

    def test_a_restart_scheduled_with_nothing_waiting_is_said_too(self):
        at = stamp(-timedelta(hours=5))
        sweep(reading(pending=False, scheduled_action="reboot", scheduled_at=at))

        (finding,) = raised(RULE)

        self.assertIn(when(moment(at)), finding["title"])
        self.assertNotIn("Waiting since", evidence(finding))

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

    def test_nothing_waiting_raises_nothing(self):
        sweep(reading(pending=False))

        self.assertEqual(raised(RULE), [])

    def test_a_reading_that_failed_raises_nothing_of_its_own(self):
        store(KIND, reachable=False)

        self.assertEqual(raised(RULE), [])

    def test_a_reading_older_than_the_slowest_sweep_is_withheld(self):
        aged = slowest_sweep_interval() + timedelta(minutes=10)
        sweep(reading(read_at=stamp(aged)), age=aged)

        with frozen(timezone.now()), uncached():
            raised_now = [
                found.rule for found in derive_findings(derive_topology(principal=EVERYTHING), principal=EVERYTHING)
            ]

        self.assertNotIn(RULE, raised_now)
