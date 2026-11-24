"""Open PR has Renovate open the pull request of an app's newer release: it
ticks the box Renovate holds the branch under, and asks a run only once the
tick lands, so the run reads it; with no box, it asks the run at once."""

from django.test import TestCase

from hq.domains.control_plane.models import OperationRequest
from hq.domains.projects.models import Project

from ..capabilities import run_capability
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..dependency_updates import asked_at
from ..security import Capability, Principal
from .test_app_updates import arranged
from .test_github_estate import store

OPEN = "app_update.open"
CONTROLLER = "controller-example"
PERSON = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
HELD = {"branch": "renovate/app", "marker": "unpend", "apps": ["app"]}


class OpenUpdateTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        Project.objects.create(
            name="Apps", slug="apps", repository_url="https://github.com/example/alpha", dependency_updates=True
        )
        arranged()

    def press(self, key="example-open-1"):
        payload = {"repository": "example/alpha", "app": "app"}
        return run_capability(OPEN, payload, principal=PERSON, idempotency_key=key).answer

    def test_a_held_branch_is_ticked_and_the_run_asked_once_the_tick_lands(self):
        store(compose=["compose/app/compose.yaml"], held_branches=[HELD])

        answer = self.press()

        self.assertTrue(answer["ok"], answer)
        tick = OperationRequest.objects.get(action=OperationRequest.Action.RECONCILE)
        self.assertEqual(
            (tick.resource.kind, tick.resource.spec),
            ("github.dashboard_box", {"repository": "example/alpha", "branch": "renovate/app", "marker": "unpend"}),
        )
        self.assertIsNone(asked_at(), "no run is asked before the box is ticked")
        claimed = claim_next_operation(CONTROLLER, capabilities=(("github.dashboard_box", "reconcile"),))
        report_operation(
            claimed["operation"]["id"],
            ControllerReport(success=True, observed_generation=tick.input["generation"]),
            controller_id=CONTROLLER,
        )
        self.assertIsNotNone(asked_at())

    def test_a_release_renovate_holds_nothing_of_asks_the_run_at_once(self):
        answer = self.press()

        self.assertTrue(answer["ok"], answer)
        self.assertIsNotNone(asked_at())
        self.assertFalse(OperationRequest.objects.exists())
