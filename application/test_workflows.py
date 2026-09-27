"""Resolution workflows derived from facts, never a second executor."""

from django.test import SimpleTestCase
from django.template.loader import render_to_string

from .action_links import ActionLink
from .ui import Insight
from .workflows import claim_identity, claim_resolution_plan, serialize_workflow


class FindingResolutionWorkflowTests(SimpleTestCase):
    def test_evidence_action_and_verification_form_one_ordered_plan(self):
        inspect = ActionLink("impact", "Trace impact", "read", "/topology/?trace")
        remedy = ActionLink(
            "remedy",
            "Request fresh sweep",
            "infrastructure_change",
            "/commands/infrastructure.controller.refresh/",
            capability="infrastructure.controller.refresh",
            recommended=True,
        )
        context = ActionLink("open", "Open connections", "read", "/connections/")

        verify = ActionLink("verify", "Recheck", "read", "/recheck/")
        plan = claim_resolution_plan(
            namespace="example.claim",
            rule="controller-sweep-stale",
            subject="controller:one",
            scope="",
            investigations=(inspect,),
            offers=(context,),
            remedies=(remedy,),
            verification=verify,
        )

        self.assertIsNotNone(plan)
        self.assertEqual(
            [step.phase for step in plan.steps], ["understand", "act", "verify"]
        )
        self.assertEqual(plan.steps[1].state, "recommended")
        self.assertEqual(
            [action.label for action in plan.steps[1].actions],
            ["Request fresh sweep", "Open connections"],
        )
        self.assertEqual(plan.outcome.kind, "claim_absent")
        self.assertEqual(
            plan.outcome.claim_id,
            claim_identity(
                "example.claim", "controller-sweep-stale", "controller:one"
            ),
        )
        self.assertEqual(plan.steps[2].actions[0].url, "/recheck/")

    def test_duplicate_routes_are_emitted_once_per_phase(self):
        action = ActionLink("open", "Open", "read", "/same/")

        plan = claim_resolution_plan(
            namespace="example.claim",
            rule="example",
            subject="resource:one",
            scope="",
            investigations=(action, action),
            offers=(),
            remedies=(),
            verification=ActionLink("verify", "Recheck", "read", "/recheck/"),
        )

        self.assertEqual(len(plan.steps[0].actions), 1)

    def test_a_claim_with_no_working_route_does_not_pretend_to_be_a_workflow(self):
        self.assertIsNone(
            claim_resolution_plan(
                namespace="example.claim",
                rule="example",
                subject="",
                scope="example.kind",
                investigations=(),
                offers=(),
                remedies=(),
                verification=None,
            )
        )

    def test_serialization_keeps_actions_and_completion_machine_readable(self):
        plan = claim_resolution_plan(
            namespace="example.claim",
            rule="example",
            subject="resource:one",
            scope="",
            investigations=(ActionLink("open", "Open", "read", "/open/"),),
            offers=(),
            remedies=(),
            verification=ActionLink("verify", "Recheck", "read", "/recheck/"),
        )

        payload = serialize_workflow(plan)

        self.assertEqual(payload["steps"][0]["actions"][0]["url"], "/open/")
        self.assertEqual(payload["outcome"]["kind"], "claim_absent")

    def test_any_domain_insight_can_render_the_shared_resolution_workflow(self):
        plan = claim_resolution_plan(
            namespace="example.utility",
            rule="changed",
            subject="account:one",
            scope="",
            investigations=(ActionLink("open", "Inspect bill", "read", "/bill/"),),
            offers=(),
            remedies=(),
            verification=ActionLink("verify", "Recheck", "read", "/recheck/"),
        )
        insight = Insight(
            "attention",
            "Utility",
            "A reading changed",
            "$1",
            "Synthetic plugin evidence.",
            workflow=plan,
        )

        html = render_to_string(
            "partials/_attention_list.html",
            {"entries": ({"item": insight, "source": "Example"},)},
        )

        self.assertIn("Steps to resolve", html)
        self.assertIn("Inspect bill", html)
        self.assertIn("Confirm the fix", html)


class WorkflowLayoutTests(SimpleTestCase):
    """A card leads with the gated remedy; the rest is one line of links."""

    def plan(self, remedies=()):
        return claim_resolution_plan(
            namespace="example.claim",
            rule="example",
            subject="resource:one",
            scope="",
            investigations=(ActionLink("open", "Show in topology", "read", "/topology/"),),
            offers=(ActionLink("open", "Open connections", "read", "/connections/"),),
            remedies=remedies,
            verification=ActionLink("verify", "Check again", "read", "/findings/"),
        )

    def test_remedies_lead_and_offers_are_only_links(self):
        from .workflows import workflow_layout

        remedy = ActionLink(
            "remedy", "Remove it", "write", "/remove/", method="POST", recommended=True
        )

        layout = workflow_layout(self.plan((remedy,)))

        self.assertEqual(layout.fix, (remedy,))
        self.assertEqual([action.label for action in layout.related], ["Open connections"])
        self.assertEqual([action.label for action in layout.impact], ["Show in topology"])
        self.assertEqual([action.label for action in layout.confirm], ["Check again"])

    def test_without_a_remedy_nothing_is_offered_as_the_fix(self):
        from .workflows import workflow_layout

        layout = workflow_layout(self.plan())

        self.assertEqual(layout.fix, ())
        self.assertTrue(layout.links)

    def test_no_plan_is_an_empty_layout(self):
        from .workflows import workflow_layout

        layout = workflow_layout(None)

        self.assertEqual((layout.fix, layout.links), ((), False))
