"""A repository's workflow runs and pull requests: read in its pass, carried
with it, asked for with it, and read by an agent like any reading."""

import json

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from mcp import Client

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.github import REPOSITORY_KIND, carry_held
from hq.platform.application.read_requests import forced_kinds
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.listing import widest_caller
from hq.platform.mcp.server import mcp

WORKFLOW_RUN_KIND = "github.workflow_run"
PULL_REQUEST_KIND = "github.open_pull_request"
NAMED = {"connection_ref": "github", "repository": "example/app"}
# As the controller sends them (controller/providers/github_test.go,
# TestGitHubRepositoryReading).
RUN = NAMED | {
    "id": 6,
    "name": "CI",
    "status": "completed",
    "conclusion": "failure",
    "event": "pull_request",
    "branch": "bump",
    "sha": "b" * 40,
    "url": "https://github.com/example/app/actions/runs/6",
    "created_at": "2030-01-05T00:00:00Z",
    "jobs": [{"name": "test", "status": "completed", "conclusion": "failure"}],
    "carried": False,
}
PULL = NAMED | {
    "number": 6,
    "title": "Update example-lib to v2",
    "head": "e" * 40,
    "branch": "renovate/example-lib-2.x",
    "base_branch": "main",
    "mergeable_state": "blocked",
    "review_state": "review_required",
    "check_runs": [{"name": "build", "status": "completed", "conclusion": "success"}],
    "carried": False,
}
CARRIED = NAMED | {"carried": True}


class CarriedTests(SimpleTestCase):
    def test_a_carried_repository_keeps_every_record_held_of_it(self):
        other = RUN | {"repository": "example/other", "id": 7}
        held = [RUN, RUN | {"branch": "main", "id": 5}, other]

        self.assertEqual(carry_held(held, [CARRIED, other | {"id": 8}]), [*held[:2], other | {"id": 8}])

    def test_a_carried_repository_nothing_is_held_of_sends_nothing(self):
        self.assertEqual(carry_held([], [CARRIED]), [])


class AskedTogetherTests(SimpleTestCase):
    def test_asking_for_any_kind_of_the_pass_asks_for_all_of_them(self):
        together = (PULL_REQUEST_KIND, REPOSITORY_KIND, WORKFLOW_RUN_KIND)
        for kind in together:
            with self.subTest(kind=kind):
                self.assertEqual(forced_kinds("", kind), together)


class AgentReadsTests(TestCase):
    def test_an_agent_reads_runs_on_a_branch_and_a_pull_requests_state(self):
        for kind, record in ((WORKFLOW_RUN_KIND, RUN), (PULL_REQUEST_KIND, PULL)):
            spec = OBSERVATIONS[kind]
            ProviderInventory.objects.create(
                kind=kind, records=spec.clean([record])[0], reachable=True, connected=True, observed_at=timezone.now()
            )

        async def read(kind):
            with set_principal(widest_caller()):
                async with Client(mcp.server) as client:
                    answer = await client.call_tool("get_resource", {"name": "readings", "identifier": kind})
            return json.loads(answer.content[0].text)

        (run,) = async_to_sync(read)(WORKFLOW_RUN_KIND)["items"]
        (pull,) = async_to_sync(read)(PULL_REQUEST_KIND)["items"]

        self.assertEqual((run["branch"], run["conclusion"], run["jobs"][0]["name"]), ("bump", "failure", "test"))
        self.assertEqual((pull["mergeable_state"], pull["review_state"]), ("blocked", "review_required"))
        self.assertEqual(pull["check_runs"][0]["conclusion"], "success")
