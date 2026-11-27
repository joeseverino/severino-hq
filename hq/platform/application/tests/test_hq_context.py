"""HQ as a subject: its page and its resource, from what HQ already holds."""

from datetime import UTC, timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.observations.host import RENDER_STATUS_KIND, UNIT_KIND
from hq.platform.application.security import cli_principal
from hq.platform.core.testing import declare, operator

from ..container_words import UP_TO_DATE, UPDATE_AVAILABLE
from ..hq_context import hq_context
from ..inventory_testing import store

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


def estate(*, containers: int = 0, timers: int = 1, deployed: str = HEAD, head: str = HEAD) -> None:
    declare("example-box", "machine", {"name": "example-box", "addresses": ["192.0.2.44"]})
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
        repository(head={"sha": head}, deployments=[{"environment": "production", "sha": deployed, "image": IMAGE}]),
    )
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
            return hq_context(request, principal=cli_principal())

    def test_a_branch_ahead_of_production_is_said(self):
        estate(deployed="e" * 40)

        found = self.context()

        self.assertTrue(found.release.behind)
        self.assertEqual(found.container.standing.update_state, UPDATE_AVAILABLE)
        # Each names the branch that is ahead.
        self.assertIn("main", found.release.standing)
        self.assertIn("main", found.container.standing.update)


@SITE
class ProblemTests(TestCase):
    def test_a_finding_about_its_controller_is_its_own(self):
        estate()
        store(
            RENDER_STATUS_KIND,
            {"renderer": "hq", "state": "read", "status": {"last_attempt": {"at": stamp(), "outcome": "current"}}},
        )
        first, second = own("192.0.2.44")
        with first, second:
            found = hq_context(principal=cli_principal())

        self.assertEqual(found.status[0], "serious")
        # A controller is what HQ sees through, so its problems are drawn there.
        self.assertEqual(set(found.parts), {"sight"})
        self.assertIn("serious", {tile.tone for tile in found.tiles})


@SITE
class PageTests(TestCase):
    def setUp(self):
        operator(self.client)
        first, second = own("192.0.2.44")
        first.start()
        second.start()
        self.addCleanup(first.stop)
        self.addCleanup(second.stop)

    def test_unreadable_units_render_as_a_sentence(self):
        estate()
        store(UNIT_KIND, reachable=False, error="")

        response = self.client.get(reverse("hq_self"))

        self.assertContains(response, "could not read")

    def test_the_machine_page_says_hq_is_up_to_date(self):
        estate()

        response = self.client.get(reverse("control_plane:machine", args=["example-box"]))

        self.assertEqual(response.context["standings"]["app"].update_state, UP_TO_DATE)


@SITE
class ResourceTests(TestCase):
    def test_an_agent_asks_hq_how_it_is(self):
        from hq.platform.mcp import services
        from hq.platform.mcp.identity import set_principal

        from ..resources import list_resource
        from ..security import cli_principal, mcp_principal

        estate()
        first, second = own("192.0.2.44")
        self.enterContext(set_principal(mcp_principal()))
        with first, second:
            direct = list_resource("hq", {}, principal=cli_principal())
            through_mcp = services.list_resource("hq")

        for found in (direct, through_mcp):
            self.assertEqual((found["count"], found["status"]), (0, "good"))
            self.assertFalse(found["release"]["behind"])
            self.assertEqual(found["where"]["container"], "example-box:app")
