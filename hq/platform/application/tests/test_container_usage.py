"""What a container uses beside its limits: memory as its sweep sampled it,
CPU as a share of one core across two sweeps, on its page and its record."""

from datetime import UTC, datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.observations.portainer import RUNTIME_KIND

from ..container_reads import get_container
from ..inventory import record_inventory
from ..security import cli_principal
from ..services import CONTAINER_KIND
from .markup import select

STARTED = "2026-10-31T11:00:00+00:00"
SAMPLED = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def runtime(*, after=timedelta(0), cpu=0, started=STARTED, **fields):
    """The runtime record a controller sends for one sample of web."""

    return {
        "connection_ref": "example-portainer",
        "environment_id": 1,
        "host": "example-box",
        "container": "web",
        "started_at": started,
        "memory_limit": 1_073_741_824,
        "cpu_limit": 1.5,
        "memory_usage": 153_092_096,
        "memory_usage_basis": "anon",
        "cpu_time": cpu,
        "usage_read_at": (SAMPLED + after).isoformat(),
        **fields,
    }


def sweep(*records):
    record_inventory({RUNTIME_KIND: {"ok": True, "records": list(records)}}, principal=cli_principal())


def fact(page, which):
    """The words of the page's usage fact ``which``; blank when it has none."""

    found = select(page.content, "data-usage", which)
    return found[0].words if found else ""


def stored():
    return ProviderInventory.objects.get(kind=RUNTIME_KIND).records[0]


class CPUShareTests(TestCase):
    def test_the_share_is_taken_across_two_sweeps_of_one_run(self):
        sweep(runtime(cpu=900_000_000_000))
        sweep(runtime(after=timedelta(minutes=1), cpu=900_000_000_000 + 30_000_000_000))

        self.assertEqual(stored()["cpu_percent"], 50.0)

    def test_one_sample_is_never_read_as_current_use(self):
        # A day of a busy core, read once: all of it is history.
        sweep(runtime(cpu=86_400_000_000_000))

        self.assertIsNone(stored()["cpu_percent"])

    def test_a_restart_between_sweeps_starts_again(self):
        sweep(runtime(cpu=900_000_000_000))
        sweep(runtime(after=timedelta(minutes=1), cpu=1_000, started="2026-10-31T12:00:30+00:00"))

        self.assertIsNone(stored()["cpu_percent"])

    def test_a_share_a_controller_sends_is_not_kept(self):
        sweep(runtime(cpu=0, cpu_percent=99.0))

        self.assertIsNone(stored()["cpu_percent"])

    def test_an_unreachable_sweep_keeps_the_last_share(self):
        sweep(runtime(cpu=0))
        sweep(runtime(after=timedelta(minutes=1), cpu=6_000_000_000))
        record_inventory({RUNTIME_KIND: {"ok": False, "records": [], "error": "down"}}, principal=cli_principal())

        self.assertEqual(stored()["cpu_percent"], 10.0)


def running():
    return {
        "name": "web",
        "stack": "web",
        "image": "example/web:1.0.0",
        "state": "running",
        "status": "Up 2 days",
        "host": "example-box",
        "host_address": "",
        "connection_ref": "example-portainer",
        "ports": [],
        "network_mode": "bridge",
        "portainer_managed": False,
    }


class UsageShownTests(TestCase):
    def setUp(self):
        ProviderInventory.objects.create(kind=CONTAINER_KIND, records=[running()], observed_at=timezone.now())
        self.resource = ManagedResource.objects.create(
            key="web-container", kind=CONTAINER_KIND, spec={"host": "example-box", "name": "web"}
        )

    def test_the_record_says_what_it_uses_beside_its_limits(self):
        sweep(runtime(cpu=0))
        sweep(runtime(after=timedelta(minutes=1), cpu=1_920_000_000))

        usage = get_container("example-box:web")["usage"]

        self.assertEqual(usage["memory_bytes"], 153_092_096)
        self.assertEqual(usage["memory_counted_as"], "anonymous memory")
        self.assertEqual(usage["memory_limit_bytes"], 1_073_741_824)
        self.assertEqual(usage["cpu_percent_of_one_core"], 3.2)
        self.assertEqual(usage["cpu_limit"], 1.5)

    def test_a_container_its_sweep_did_not_sample_has_no_usage(self):
        sweep(runtime(memory_usage=0, memory_usage_basis="", cpu_time=0, usage_read_at=None))

        self.assertIsNone(get_container("example-box:web")["usage"])

    def test_its_page_shows_memory_against_its_limit_and_cpu_once_measured(self):
        user = get_user_model().objects.create_superuser("operator", "operator@example.com", "pw")
        self.client.force_login(user)
        sweep(runtime(cpu=0))

        page = self.client.get(self.resource.get_absolute_url())
        first = fact(page, "cpu")

        sweep(runtime(after=timedelta(minutes=1), cpu=1_920_000_000))
        page = self.client.get(self.resource.get_absolute_url())

        self.assertIn("146 MB", fact(page, "memory"))
        self.assertIn("1.0 GB", fact(page, "memory"))
        self.assertNotIn("%", first)
        self.assertIn("3.2%", fact(page, "cpu"))
