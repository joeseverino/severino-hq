"""Resolution workflows derived from facts, never a second executor."""

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from ..action_links import ActionLink
from ..ui import Insight
from ..workflows import claim_identity, claim_resolution_plan, serialize_workflow
from .markup import actions, hrefs

REMEDY = ActionLink(
    "remedy",
    "Request fresh sweep",
    "infrastructure_change",
    "/commands/infrastructure.controller.refresh/",
    capability="infrastructure.controller.refresh",
    recommended=True,
)
VERIFY = ActionLink("verify", "Recheck", "read", "/recheck/")


def plan_of(remedies=(REMEDY,), verification=VERIFY):
    return claim_resolution_plan(
        namespace="example.claim",
        rule="controller-sweep-stale",
        subject="controller:one",
        scope="",
        remedies=remedies,
        verification=verification,
    )


class FindingResolutionWorkflowTests(SimpleTestCase):
    def test_a_plan_is_its_remedy_then_the_check_that_confirms_it(self):
        plan = plan_of()

        self.assertEqual([step.phase for step in plan.steps], ["act", "verify"])
        self.assertEqual(plan.steps[0].state, "recommended")
        self.assertEqual(plan.steps[0].actions, (REMEDY,))
        self.assertEqual(plan.steps[1].actions[0].url, "/recheck/")
        self.assertEqual(plan.outcome.kind, "claim_absent")
        self.assertEqual(
            plan.outcome.claim_id,
            claim_identity("example.claim", "controller-sweep-stale", "controller:one"),
        )

    def test_a_remedy_listed_twice_is_offered_once(self):
        self.assertEqual(len(plan_of(remedies=(REMEDY, REMEDY)).steps[0].actions), 1)

    def test_a_fix_made_by_hand_keeps_its_check(self):
        plan = plan_of(remedies=())

        self.assertEqual([step.phase for step in plan.steps], ["verify"])
        self.assertEqual(plan.steps[0].state, "available")

    def test_nothing_to_run_and_nothing_to_check_is_no_plan(self):
        self.assertIsNone(plan_of(remedies=(), verification=None))

    def test_serialization_keeps_actions_and_completion_machine_readable(self):
        payload = serialize_workflow(plan_of())

        self.assertEqual(payload["steps"][0]["actions"][0]["url"], REMEDY.url)
        self.assertEqual(payload["outcome"]["kind"], "claim_absent")

    def test_any_domain_insight_can_render_the_shared_resolution_workflow(self):
        insight = Insight(
            "attention",
            "Utility",
            "A reading changed",
            "$1",
            "Synthetic plugin evidence.",
            workflow=plan_of(),
        )

        html = render_to_string(
            "partials/_attention_list.html",
            {"entries": ({"item": insight, "source": "Example"},)},
        )

        offered = hrefs(html) + actions(html)
        self.assertIn(REMEDY.url, offered)
        self.assertIn(VERIFY.url, offered)
