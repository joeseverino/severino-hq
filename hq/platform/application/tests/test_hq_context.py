"""HQ as a subject: its page and its resource, from what HQ already holds."""

import importlib
from datetime import UTC, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.observations.host import RENDER_STATUS_KIND, UNIT_KIND
from hq.domains.jobs.models import Job
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow, distinct

from ..container_words import UP_TO_DATE, UPDATE_AVAILABLE
from ..drawings import Dot, Dots
from ..hq_context import hq_context
from ..inventory_testing import store
from .markup import all_elements, links, select, text, with_class

# The package exports a function named work, so the module is named by its path.
work_module = importlib.import_module("hq.platform.application.hq_context.work")

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


def repository(name: str = "example/hq", **fields) -> dict:
    """A repository as GitHub's reading reports it: by default the one HQ is built from."""

    return {
        "connection_ref": "github", "repository": name, "url": f"https://github.com/{name}",
        "default_branch": "main", "head": {"sha": HEAD}, **fields,
    }  # fmt: skip


def tailnet(**fields) -> None:
    """``example-box``, where HQ runs, as the tailnet reports it."""

    store("tailscale.device", {"name": "example-box", "online": True, **fields})


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
    store("github.repository", repository(deployments=[{"environment": "production", "sha": deployed}]))
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
        self.assertEqual((timer.runs, timer.result), ("Delete old routine log entries", "Done"))
        # What HQ has no words for is named by its unit, an identifier.
        (web,) = (job for job in found.work.jobs if job.unit.endswith("-web.service"))
        self.assertEqual([(job.runs, job.runs_unit) for job in (timer, web)], [(timer.runs, False), (web.unit, True)])
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

    def test_a_reported_connection_is_never_called_unconnected_before_its_first_reading(self):
        from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
        from hq.domains.control_plane.models import ProviderConnection

        ProviderConnection.objects.create(
            connection_ref="example-portainer",
            controller_id="example-controller",
            provider="portainer",
            endpoint="https://portainer.example.com",
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

        seen = self.context().sight

        self.assertNotIn(CONNECTION_LABELS["portainer"], seen.unconnected)
        self.assertIn(CONNECTION_LABELS["portainer"], dict(seen.groups))

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


@SITE
class ProblemTests(TestCase):
    def test_no_problems_is_running(self):
        estate()
        first, second = own("192.0.2.44")
        with first, second:
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
        # A controller is what HQ sees through, so its problems are drawn there.
        self.assertEqual(set(found.parts), {"sight"})
        self.assertEqual({tile.label: tile.tone for tile in found.tiles}["Sees"], "serious")

    def test_only_problems_about_hq_are_its_own(self):
        estate()
        tailnet(authorized=False)
        store(
            "github.repository",
            repository(deployments=[{"environment": "production", "sha": HEAD}], alerts={"code_scanning": {"high": 1}}),
            repository("example/other", alerts={"code_scanning": {"critical": 1}}),
        )
        first, second = own("192.0.2.44")
        with first, second:
            found = hq_context()

        self.assertEqual(
            [item["label"] for item in found.problems],
            ["example-box is waiting for tailnet approval", "1 serious alert in hq"],
        )
        self.assertEqual(found.status, ("serious", "1 urgent · 1 needs attention"))
        # Each beside the fact it explains: the machine where HQ runs, the repository its release.
        self.assertEqual(found.parts, ("where", "release"))
        tones = {tile.label: tile.tone for tile in found.tiles}
        self.assertEqual((tones["Runs on"], tones["Release"]), ("serious", "attention"))


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
        for words in ("Release", "Runs on", "Reached through", "Jobs", "Readings"):
            self.assertContains(response, words)
        self.assertContains(response, "Delete old routine log entries")

    def test_a_problem_is_drawn_under_the_part_it_is_about_and_the_status_leads_to_it(self):
        estate()
        tailnet(update_available=True)

        response = self.client.get(reverse("hq_self"))

        self.assertIn(("1 needs attention", "#hq-where-heading"), links(response))
        # Not urgent, so a line under its part and nothing framed above the page.
        self.assertEqual(select(response, "id", "problems-here"), [])
        page = all_elements(response)
        where = next(index for index, element in enumerate(page) if element.attrs.get("id") == "hq-where-heading")
        line = next(index for index, element in enumerate(page) if "problem-line" in element.classes)
        work = next(index for index, element in enumerate(page) if element.attrs.get("id") == "hq-work-heading")
        self.assertLess(where, line)
        self.assertLess(line, work)

    def test_an_urgent_problem_is_framed_above_the_page(self):
        estate()
        tailnet(authorized=False)

        response = self.client.get(reverse("hq_self"))

        self.assertIn(("1 urgent", "#problems-here"), links(response))
        self.assertIn("1 urgent problem", text(response))
        page = all_elements(response)
        framed = next(index for index, element in enumerate(page) if element.attrs.get("id") == "problems-here")
        facts = next(index for index, element in enumerate(page) if "fact-band" in element.classes)
        self.assertLess(framed, facts)
        self.assertEqual(with_class(response, "problem-line"), [])

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
        def arrange(count):
            ManagedResource.objects.all().delete()
            estate(containers=count, timers=count)

        def draw(_count):
            response = self.client.get(reverse("hq_self"))
            self.assertEqual(response.status_code, 200)
            return response

        # Every timer is a row of HQ's work; the other containers are not drawn.
        assert_cost_does_not_grow(
            self, sizes=(1, 12), arrange=arrange, draw=draw, rows=distinct(r"\bexample-\d+\.timer\b")
        )


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


class ScheduledWorkTests(TestCase):
    """The page's scheduled work: derived from the declarations and their jobs."""

    def setUp(self):
        # The host's declarations only; an installed extension's are its own suite's.
        self.enterContext(mock.patch("hq.platform.application.outbound_work.declared_work", return_value={}))

    def ran(self, kind, ago, state=Job.State.SUCCEEDED):
        job = Job.objects.create(kind=kind, label=kind, state=state, finished_at=timezone.now() - ago)
        Job.objects.filter(pk=job.pk).update(created_at=timezone.now() - ago)
        return job

    def rows(self):
        return {item.name: item for item in work_module._scheduled_work()}

    def test_a_piece_of_work_shows_its_recent_runs_oldest_first_and_when_it_is_due(self):
        oldest = self.ran("audit.prune", timedelta(days=2))
        failed = self.ran("audit.prune", timedelta(days=1), state=Job.State.FAILED)
        newest = self.ran("audit.prune", timedelta(hours=2))

        row = self.rows()["audit.prune"]

        self.assertEqual((row.every, row.result, row.tone), ("Daily", "Done", "good"))
        self.assertEqual([dot.filled for dot in row.runs.shown], [True, False, True])
        self.assertEqual(
            [dot.url for dot in row.runs.shown],
            [reverse("jobs:status", args=[job.pk]) for job in (oldest, failed, newest)],
        )
        self.assertAlmostEqual(row.next_due, newest.finished_at + timedelta(days=1), delta=timedelta(seconds=1))

    def test_work_never_done_is_due_now_and_says_so(self):
        row = self.rows()["content.sync"]

        self.assertEqual((row.every, row.result, row.next_due, row.runs), ("Daily", "Not done yet", None, None))

    def test_every_piece_with_a_cadence_has_a_row_and_none_without(self):
        self.assertEqual(set(self.rows()), {"audit.prune", "content.sync", "registry.refresh", "sessions.clear"})

    def test_the_cost_does_not_grow_with_the_jobs(self):
        def arrange(count):
            Job.objects.filter(kind="content.sync").delete()
            for hours in range(count):
                self.ran("content.sync", timedelta(hours=hours))

        # Twelve runs are what a row shows, so twelve is the most it can prove.
        assert_cost_does_not_grow(
            self,
            sizes=(1, 12),
            arrange=arrange,
            draw=lambda _count: self.rows(),
            rows=lambda rows: len(rows["content.sync"].runs.shown),
        )
        arrange(40)
        self.assertEqual(len(self.rows()["content.sync"].runs.shown), 12)

    def test_the_resource_carries_the_rows_without_the_drawing(self):
        self.ran("sessions.clear", timedelta(hours=1))

        (row,) = [
            item
            for item in work_module.work_json(work_module.Work(scheduled=tuple(self.rows().values())))["scheduled"]
            if item["name"] == "sessions.clear"
        ]

        self.assertEqual((row["label"], row["every"], row["result"]), ("Delete expired sessions", "Daily", "Done"))
        self.assertEqual(len(row["recent"]), 1)


class OrderedDotsTests(SimpleTestCase):
    def test_a_history_keeps_its_order_and_a_part_of_a_whole_leads_with_what_it_counts(self):
        dots = (Dot(filled=True, tip="a"), Dot(filled=False, tip="b"), Dot(filled=True, tip="c"))

        self.assertEqual([dot.tip for dot in Dots(dots, ordered=True).shown], ["a", "b", "c"])
        self.assertEqual([dot.tip for dot in Dots(dots).shown], ["a", "c", "b"])
