"""What an action costs, and that it answers before the work it asks for.

An action that asks for work outside the request stores the ask and answers:
a constant number of queries whatever the estate holds, a 202 with where to
follow the work, and nothing read, run or changed by the time it has answered.
"""

from django.test import Client, TestCase

from hq.domains.control_plane.models import ProviderInventory, ReadRequest
from hq.domains.jobs.models import Job
from hq.platform.application.tests.host_only import host_only
from hq.platform.core.bench import Seeded, seed
from hq.platform.core.management.commands.bench_pages import (
    ACTIONS,
    UNSAFE,
    _actions,
    _controller_arriving,
    _held_outside,
    _post,
)

from .estate_sizes import SMALL, at_both_sizes

SCRIPT = {"X-Requested-With": "XMLHttpRequest"}

# The actions that ask for work outside the request.
BUDGETED = ("watching_refresh", "projects:refresh", "control_plane:read_now", "control_plane:visit")


class ActionBudgetTests(TestCase):
    def cost(self, seeded: Seeded) -> dict[str, int]:
        client = Client(headers=SCRIPT)
        client.force_login(seeded.user)
        exercised, _left = _actions(seeded)
        costs = {}
        with host_only(), _held_outside(), _controller_arriving():
            for name, url, action in exercised:
                if name not in BUDGETED:
                    continue
                # Once to warm what a process caches, then the measured post.
                _post(client, url, action, seeded)
                response, _took, _size, statements = _post(client, url, action, seeded)
                self.assertIn(response.status_code, (200, 202), name)
                costs[name] = len(statements)
        return costs

    def test_an_action_costs_the_same_at_any_size(self):
        small, large = at_both_sizes(self.cost)

        self.assertEqual(set(large), set(BUDGETED))
        self.assertEqual(large, small)

    def test_every_action_that_asks_for_outside_work_is_benched(self):
        self.assertLessEqual(set(BUDGETED), set(ACTIONS))
        self.assertFalse(set(ACTIONS) & set(UNSAFE))


class AnswersBeforeTheWorkTests(TestCase):
    """The request has answered while the work it asked for is still to do."""

    def setUp(self):
        self.seeded = seed(SMALL)
        self.client = Client(headers=SCRIPT)
        self.client.force_login(self.seeded.user)
        self.exercised = {name: (url, action) for name, url, action in _actions(self.seeded)[0]}
        self.held = _held_outside()
        self.held.__enter__()
        self.addCleanup(self.held.__exit__, None, None, None)

    def post(self, name: str):
        url, action = self.exercised[name]
        action.prepare(self.seeded)
        return self.client.post(url, action.data(self.seeded))

    def stored(self) -> dict[str, object]:
        return dict(ProviderInventory.objects.values_list("kind", "updated_at"))

    def test_a_read_is_asked_for_and_not_taken(self):
        for name in ("watching_refresh", "control_plane:read_now"):
            with self.subTest(name):
                before = self.stored()
                asked = ReadRequest.objects.count()

                response = self.post(name)

                answer = response.json()
                self.assertEqual((response.status_code, answer["live"]), (202, True))
                self.assertIn(answer["state"], ("queued", "running"))
                self.assertEqual(ReadRequest.objects.count(), asked + 1)
                # No reading moved: the controller has not been here.
                self.assertEqual(self.stored(), before)
                self.assertTrue(self.client.get(answer["status"]).json()["live"])

    def test_a_job_is_recorded_and_its_work_has_not_run(self):
        response = self.post("projects:refresh")

        answer = response.json()
        job = Job.objects.get(kind="project.refresh")
        self.assertEqual((response.status_code, answer["state"]), (202, "queued"))
        self.assertEqual((job.state, job.started_at, job.result), ("queued", None, {}))
        self.assertFalse(ReadRequest.objects.exists())
