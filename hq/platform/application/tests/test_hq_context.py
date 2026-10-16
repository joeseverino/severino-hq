"""HQ as a subject: its page and its resource, from what HQ already holds."""

from datetime import UTC, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.observations.host import RENDER_STATUS_KIND, UNIT_KIND

from ..container_words import UP_TO_DATE, UPDATE_AVAILABLE
from ..hq_context import hq_context
from ..inventory_testing import store
from ..ui import Insight

DIGEST = "sha256:" + "a" * 64
PREVIOUS = "sha256:" + "b" * 64
IMAGE = f"ghcr.io/example/hq/composition@{DIGEST}"
HEAD = "c" * 40
SITE = override_settings(
    SEVERINO_SITE_HOST="hq.example.com",
    ALLOWED_HOSTS=["hq.example.com", "testserver"],
    SEVERINO_HQ_SOURCE="https://github.com/example/hq",
    SEVERINO_HQ_REVISION=HEAD,
)


def stamp(age: timedelta = timedelta(0)) -> str:
    return (timezone.now() - age).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def own(*addresses):
    """This process routes from ``addresses`` and runs in the container ``app``."""

    return (
        mock.patch(
            "hq.platform.application.hq_self.host_addresses",
            return_value=frozenset({"127.0.0.1", "::1", *addresses}),
        ),
        mock.patch("hq.platform.application.request_path.own_container_id", return_value="0123456789ab"),
    )


def running(name, image, container_id=""):
    return {
        "name": name, "stack": name, "image": image, "state": "running", "status": "Up 2 days (healthy)",
        "host": "example-box", "host_address": "", "connection_ref": "example-portainer", "ports": [],
        "network_mode": "bridge", "portainer_managed": False, "id": container_id,
    }  # fmt: skip


def units(count: int = 1) -> list[dict]:
    """The web service, and ``count`` timers each starting a declared job."""

    found = [
        {
            "unit": "severino-hq-example-web.service", "load": "loaded", "file_state": "enabled",
            "active": "active", "sub": "running", "started_at": stamp(timedelta(days=1)), "read_at": stamp(),
        },
        {
            "unit": "severino-hq-job@audit.prune.service", "load": "loaded", "file_state": "static",
            "active": "inactive", "sub": "dead", "result": "success", "started_at": stamp(timedelta(hours=2)),
            "read_at": stamp(),
        },
    ]  # fmt: skip
    found.extend(
        {
            "unit": f"severino-hq-example-{index}.timer", "load": "loaded", "file_state": "enabled",
            "active": "active", "sub": "waiting", "last_trigger_at": stamp(timedelta(hours=2)),
            "next_elapse_at": stamp(timedelta(hours=-22)), "activates": "severino-hq-job@audit.prune.service",
            "read_at": stamp(),
        }
        for index in range(count)
    )  # fmt: skip
    return found


def estate(*, containers: int = 0, timers: int = 1, deployed: str = HEAD) -> None:
    ManagedResource.objects.create(
        key="example-box", kind="machine", spec={"name": "example-box", "addresses": ["192.0.2.44"]}
    )
    store(
        "portainer.container",
        running("app", IMAGE, "0123456789ab"),
        *(running(f"other-{index}", "example/other:1.0.0") for index in range(containers)),
    )
    store(
        "portainer.image",
        {
            "connection_ref": "example-portainer", "host": "example-box", "id": DIGEST, "tags": [IMAGE],
            "created_at": stamp(timedelta(hours=3)), "size": 10,
            "containers": [{"container": "app", "reference": IMAGE}],
        },
        {
            "connection_ref": "example-portainer", "host": "example-box", "id": PREVIOUS,
            "tags": [f"ghcr.io/example/hq/composition@{PREVIOUS}"], "created_at": stamp(timedelta(days=2)),
            "size": 10, "containers": [],
        },
        {
            "connection_ref": "example-portainer", "host": "example-box", "id": "sha256:" + "d" * 64,
            "tags": ["example/old:1"], "created_at": stamp(timedelta(days=9)), "size": 10, "containers": [],
        },
    )  # fmt: skip
    store(
        "github.repository",
        {
            "connection_ref": "github", "repository": "example/hq", "url": "https://github.com/example/hq",
            "default_branch": "main", "head": {"sha": HEAD},
            "deployments": [{"environment": "production", "sha": deployed}],
        },
    )  # fmt: skip
    store(UNIT_KIND, *units(timers))
    store(
        RENDER_STATUS_KIND,
        {
            "renderer": "hq", "state": "read",
            "status": {"last_attempt": {"at": stamp(), "outcome": "current"}, "last_success": {"at": stamp(), "rendered_at": stamp()}},
        },
    )  # fmt: skip


@SITE
class ProjectionTests(TestCase):
    def context(self, request=None):
        first, second = own("192.0.2.44")
        with first, second:
            return hq_context(request)

    def test_hq_knows_its_machine_container_release_and_work(self):
        estate()

        found = self.context()

        self.assertEqual(found.machine.name, "example-box")
        self.assertEqual(found.container.running.name, "app")
        self.assertEqual(
            (found.release.short, found.release.behind, found.release.runs_deployed), (HEAD[:7], False, True)
        )
        (timer,) = (job for job in found.work.jobs if job.unit.endswith(".timer"))
        self.assertEqual((timer.runs, timer.result), ("Delete old routine log entries", "Succeeded"))
        self.assertEqual([job.unit for job in found.work.services], ["severino-hq-example-web.service"])
        self.assertEqual([item.result for item in found.work.renderers], ["Already current"])
        self.assertEqual([item["label"] for item in found.problems], [])
        self.assertEqual([tile.label for tile in found.tiles], ["Release", "Runs on", "Sees"])

    def test_a_branch_ahead_of_production_is_said(self):
        estate(deployed="e" * 40)

        found = self.context()

        self.assertTrue(found.release.behind)
        self.assertFalse(found.release.runs_deployed)
        self.assertEqual(found.release.standing, "main is ahead")
        self.assertEqual(found.container.standing.update_state, UPDATE_AVAILABLE)
        self.assertEqual(found.container.standing.update, "main is ahead")

    def test_unreadable_units_are_said_and_nothing_else_breaks(self):
        estate()
        store(UNIT_KIND, reachable=False, error="no answer from systemd")

        found = self.context()

        self.assertEqual(found.work.jobs, ())
        self.assertIn("could not read HQ's background jobs: no answer from systemd", found.work.unread)
        self.assertEqual([item.name for item in found.work.renderers], ["hq"])

    def test_a_reading_past_its_cadence_is_out_of_date(self):
        estate()
        store("github.repository", age=timedelta(days=30))

        rows = {row.kind: row for row in self.context().sight.rows}

        self.assertTrue(rows["github.repository"].stale)
        self.assertFalse(rows[UNIT_KIND].stale)
        self.assertGreaterEqual(self.context().sight.stale, 1)

    def test_a_reading_that_needs_no_connection_is_listed_and_never_called_unconnected(self):
        from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS

        from ..hq_context import KEYLESS

        seen = self.context().sight
        groups = dict(seen.groups)

        self.assertIn("registry.vulnerabilities", {row.kind for row in groups[KEYLESS]})
        self.assertEqual(seen.groups[-1][0], KEYLESS)
        self.assertLessEqual(set(seen.unconnected), set(CONNECTION_LABELS.values()))
        self.assertGreaterEqual(seen.never, 1)
        (tile,) = (tile for tile in self.context().tiles if tile.label == "Sees")
        self.assertIn(f"{seen.never} never read", tile.detail)

    def test_no_container_is_named_hq_without_evidence(self):
        estate()
        store("portainer.container", running("app", IMAGE), running("twin", IMAGE))

        with mock.patch("hq.platform.application.request_path.own_container_id", return_value=""):
            found = hq_context()

        self.assertIsNone(found.container)


def entry(item: Insight) -> dict:
    return {"source_id": "hq.example", "source": "Example", "item": item}


@SITE
class ProblemTests(TestCase):
    def test_no_problems_is_running(self):
        estate()
        first, second = own("192.0.2.44")
        with first, second, mock.patch("hq.platform.application.problems._host_entries", return_value=()):
            found = hq_context()

        self.assertEqual((found.problems, found.status), ((), ("good", "Running")))

    def test_a_finding_about_its_controller_is_its_own(self):
        estate()
        store(
            RENDER_STATUS_KIND,
            {"renderer": "hq", "state": "read", "status": {"last_attempt": {"at": stamp(), "outcome": "current"}}},
        )
        first, second = own("192.0.2.44")
        with first, second:
            found = hq_context()

        self.assertTrue(any("credentials" in item["label"] for item in found.problems))
        self.assertEqual(found.status[0], "serious")

    def test_only_problems_about_hq_are_its_own(self):
        from ..ui import ActionLink

        estate()
        machine = reverse("control_plane:machine", args=["example-box"])
        entries = (
            entry(
                Insight(
                    status="serious", eyebrow="Finding", title="A job failed on example-box", value="", body="",
                    key="about-machine",
                    subject=ActionLink("subject", "example-box", "read", machine),
                )
            ),
            entry(
                Insight(
                    status="attention", eyebrow="GitHub", title="Alerts", value="", body="", key="about-repo",
                    url="https://github.com/example/hq/security",
                )
            ),
            entry(
                Insight(
                    status="serious", eyebrow="GitHub", title="Elsewhere", value="", body="", key="about-other",
                    url="https://github.com/example/other",
                )
            ),
        )  # fmt: skip
        first, second = own("192.0.2.44")
        with first, second, mock.patch("hq.platform.application.problems._host_entries", return_value=entries):
            found = hq_context()

        self.assertEqual([item["label"] for item in found.problems], ["A job failed on example-box", "Alerts"])
        self.assertEqual(found.status, ("serious", "Has a problem"))


@SITE
class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="unused-password"))
        first, second = own("192.0.2.44")
        first.start()
        second.start()
        self.addCleanup(first.stop)
        self.addCleanup(second.stop)

    def test_the_page_says_each_part(self):
        estate()

        response = self.client.get(reverse("hq_self"))

        self.assertEqual(response.status_code, 200)
        for text in ("Release", "Where it runs", "How you reached it", "Its own work", "What it can see"):
            self.assertContains(response, text)
        self.assertContains(response, "Delete old routine log entries")

    def test_unreadable_units_render_as_a_sentence(self):
        estate()
        store(UNIT_KIND, reachable=False, error="")

        response = self.client.get(reverse("hq_self"))

        self.assertContains(response, "The controller could not read HQ&#x27;s background jobs.")

    def test_the_machine_page_says_hq_is_up_to_date(self):
        estate()

        response = self.client.get(reverse("control_plane:machine", args=["example-box"]))

        self.assertEqual(response.context["standings"]["app"].update_state, UP_TO_DATE)
        self.assertContains(response, "sudo docker image prune -a")

    def test_it_is_in_the_system_menu(self):
        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, f'href="{reverse("hq_self")}"')

    def test_the_cost_does_not_grow_with_its_work_or_the_estate(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries(containers, timers):
            ManagedResource.objects.all().delete()
            estate(containers=containers, timers=timers)
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(reverse("hq_self")).status_code, 200)
            return len(captured)

        self.assertEqual(queries(1, 1), queries(20, 12))


@SITE
class ResourceTests(TestCase):
    def test_an_agent_asks_hq_how_it_is(self):
        from hq.platform.mcp import services
        from hq.platform.mcp.identity import reset_principal, set_principal

        from ..resources import list_resource
        from ..security import cli_principal, mcp_principal

        estate()
        first, second = own("192.0.2.44")
        bound = set_principal(mcp_principal())
        self.addCleanup(reset_principal, bound)
        with first, second:
            direct = list_resource("hq", {}, principal=cli_principal())
            through_mcp = services.list_resource("hq")

        for found in (direct, through_mcp):
            self.assertEqual((found["count"], found["status"]), (0, "good"))
            self.assertEqual(found["release"]["revision"], HEAD)
            self.assertFalse(found["release"]["behind"])
            self.assertEqual(found["where"]["container"], "example-box:app")
            self.assertIn("Delete old routine log entries", [job["runs"] for job in found["work"]["jobs"]])
            self.assertIsNone(found["reached"])  # no request to describe

    def test_unknown_filters_are_refused(self):
        from ..resources import InvalidResourceInput, list_resource
        from ..security import cli_principal

        with self.assertRaises(InvalidResourceInput):
            list_resource("hq", {"limit": 1}, principal=cli_principal())
