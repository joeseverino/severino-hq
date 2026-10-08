"""What an action costs, and that it answers before the work it asks for.

An action that asks for work outside the request stores the ask and answers:
a constant number of queries whatever the estate holds, a 202 with where to
follow the work, and nothing read, run or changed by the time it has answered.
"""

from unittest import mock

from django.test import Client, TestCase

from hq.domains.control_plane.models import ProviderInventory, ReadRequest
from hq.domains.jobs.models import Job
from hq.platform.core.bench import seed
from hq.platform.core.management.commands.bench_pages import ACTIONS, UNSAFE, _actions, _held_outside, _post

SMALL, LARGE = 0.05, 0.25
SCRIPT = {"X-Requested-With": "XMLHttpRequest"}

# The queries each converted action makes, at any size of estate.
BUDGETS = {
    "watching_refresh": 19,
    "projects:refresh": 9,
    "control_plane:read_now": 16,
    "control_plane:visit": 17,
}


def _extensions_out():
    return (
        mock.patch("hq.platform.application.domains.extension_domains", return_value=()),
        mock.patch("hq.platform.application.plugins.plugin_connection_specs", return_value=()),
    )


class ActionBudgetTests(TestCase):
    def cost(self, scale: float) -> dict[str, int]:
        seeded = seed(scale)
        client = Client(headers=SCRIPT)
        client.force_login(seeded.user)
        exercised, _left = _actions(seeded)
        costs = {}
        first, second = _extensions_out()
        with first, second, _held_outside():
            for name, url, action in exercised:
                if name not in BUDGETS:
                    continue
                # Once to warm what a process caches, then the measured post.
                _post(client, url, action, seeded)
                response, _took, _size, statements = _post(client, url, action, seeded)
                self.assertIn(response.status_code, (200, 202), name)
                costs[name] = len(statements)
        return costs

    def test_a_small_estate_costs_the_pinned_queries(self):
        self.assertEqual(self.cost(SMALL), BUDGETS)

    def test_a_large_estate_costs_the_same(self):
        self.assertEqual(self.cost(LARGE), BUDGETS)

    def test_every_action_that_asks_for_outside_work_is_benched(self):
        self.assertLessEqual(set(BUDGETS), set(ACTIONS))
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
