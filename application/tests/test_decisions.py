"""One presentation of an owner's actions, without changing the emitted facts."""

from copy import deepcopy
from dataclasses import asdict

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils.html import escape

from application.decisions import decision
from application.workflow_contracts import ActionLink, WorkflowOutcome, WorkflowPlan, WorkflowStep


class DecisionTests(SimpleTestCase):
    def item(self, *, actions=(), steps=()):
        return {
            "label": "Example needs a decision", "status": "attention",
            "source": "Example", "detail": "An owner's complete evidence.",
            "count": 1, "actions": [asdict(action) for action in actions],
            "workflow": asdict(WorkflowPlan(
                "example", "Steps to resolve", steps,
                WorkflowOutcome("claim_absent", "example", "A fresh check confirms it."),
            )) if steps else None,
        }

    def test_the_first_available_step_leads_without_mutating_or_repeating_it(self):
        action = ActionLink("fix", "Review change", "read", "/example/review/", recommended=True)
        verify = ActionLink("verify", "Recheck", "write", "/example/check/", method="POST")
        item = self.item(steps=(
            WorkflowStep("act", "Fix it", "", "recommended", (action,)),
            WorkflowStep("verify", "Confirm", "Check the outcome", "after_action", (verify,)),
        ))
        before = deepcopy(item)
        shown = decision(item)
        self.assertEqual(item, before)
        self.assertEqual(shown["actions"], [asdict(action)])
        self.assertEqual([step["phase"] for step in shown["workflow"]["steps"]], ["verify"])
        html = render_to_string("partials/_work_queue.html", {"items": [item]})
        self.assertEqual(html.count('href="/example/review/"'), 1)
        self.assertIn('formaction="/example/check/"', html)
        self.assertIn('form="hq-post"', html)
        self.assertNotIn("<form", html)
        # The promoted action stands outside the disclosure, never folded into it.
        self.assertNotIn("Review change", html[html.index("<details") : html.index("</details>")])
        self.assertGreater(html.index(escape(item["detail"])), html.index("<details"))

    def test_owner_actions_win_and_only_identical_method_and_url_are_removed(self):
        get = ActionLink("open", "Open", "read", "/example/")
        post = ActionLink("run", "Run", "write", "/example/", method="POST")
        shown = decision(self.item(actions=(get,), steps=(
            WorkflowStep("act", "Act", "Keep this explanation", "recommended", (get, post)),
        )))
        self.assertEqual(shown["actions"], [asdict(get)])
        self.assertEqual(shown["workflow"]["steps"][0]["actions"], [asdict(post)])
        self.assertEqual(shown["workflow"]["steps"][0]["summary"], "Keep this explanation")

    def test_a_blocked_or_later_step_is_never_promoted(self):
        action = ActionLink("run", "Run", "write", "/example/", method="POST")
        for state in ("blocked", "after_action"):
            with self.subTest(state=state):
                item = self.item(steps=(WorkflowStep("act", "Act", "", state, (action,)),))
                self.assertEqual(decision(item)["actions"], [])
                self.assertEqual(decision(item)["workflow"], item["workflow"])

    def test_a_fully_promoted_plan_does_not_leave_an_empty_disclosure(self):
        action = ActionLink("open", "Review", "read", "/example/")
        shown = decision(self.item(steps=(WorkflowStep("act", "Act", "", "available", (action,)),)))
        self.assertIsNone(shown["workflow"])

    def test_destructive_post_actions_keep_the_shared_safety_and_escaping(self):
        action = ActionLink("remove", "Remove <example>", "destructive", "/example/remove/", method="POST")
        html = render_to_string("partials/_action_links.html", {"actions": (action,)})
        self.assertIn('formaction="/example/remove/"', html)
        self.assertIn("ghost danger", html)
        self.assertIn("Remove &lt;example&gt;", html)
        self.assertNotIn('href="/example/remove/"', html)
