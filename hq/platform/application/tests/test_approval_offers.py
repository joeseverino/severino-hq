"""A held request is offered for a decision only to someone HQ lets decide it.

The rule that refuses a decision (``approvals.refusals``) is the one each page
asks before it draws a decision's button, so a person is never shown a button
that would be refused, and a forged press is still refused.
"""

import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, tag
from django.urls import URLPattern, get_resolver, reverse

from hq.domains.control_plane.approval_views import ApprovalDecisionView
from hq.domains.control_plane.models import ApprovalRequest
from hq.platform.application.agent_access import AgentAccessCommand, set_agents_paused
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.derived_reads import list_action_items
from hq.platform.application.markup_testing import text
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.roles import Role, assign
from hq.platform.application.security import mcp_principal, web_principal

from .test_approvals import POLICY_KEY, declare_policy, policy_document, update_payload
from .writers import keyed

ROOT = Path(__file__).resolve().parents[4]
# What the held request's queue item and card are titled by.
ASKED = "mcp-service-account"


def _decide_urls(held) -> dict[str, str]:
    """Each decision's address, by its path in the decide route."""

    return {
        decision: reverse("control_plane:approval_decide", kwargs={"approval_id": held.pk, "decision": decision})
        for decision in ("approve", "reject")
    }


class DecisionOfferTests(TestCase):
    """Who is offered Approve and Reject, on every page that draws them."""

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_policy()
        answer = execute_capability(
            "infrastructure.resource.update",
            keyed(update_payload(policy_document("group:elsewhere"))),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        self.assertTrue(answer["ok"], answer)
        self.held = ApprovalRequest.objects.get()
        users = get_user_model().objects
        self.operator = users.create_user(username="example-operator")
        self.viewer = users.create_user(username="example-viewer")
        assign(self.viewer, Role.VIEWER)

    def _doors(self, user) -> dict[str, str]:
        """Every place that draws a held request's decision, as ``user`` reads it: its markup, or its machine answer."""

        self.client.force_login(user)
        drawn = {
            "Needs you": self.client.get(reverse("action_items")).content.decode(),
            "Dashboard": self.client.get(reverse("dashboard")).content.decode(),
            "Approval card": self.client.get(
                reverse("core:approval_entry", kwargs={"approval_id": self.held.pk}), follow=True
            ).content.decode(),
            "Action items, as a machine reads them": repr(list_action_items(principal=web_principal(user))),
        }
        self.client.logout()
        return drawn

    def _offered(self, user) -> set[str]:
        """What each door draws of the held request's decisions, the same at every door."""

        urls = _decide_urls(self.held)
        drawn = {
            door: frozenset(decision for decision, url in urls.items() if url in page)
            for door, page in self._doors(user).items()
        }
        self.assertEqual(len(set(drawn.values())), 1, drawn)
        return set(next(iter(drawn.values())))

    def test_an_operator_is_offered_both_decisions_and_takes_one(self):
        self.assertEqual(self._offered(self.operator), {"approve", "reject"})

        self.client.force_login(self.operator)
        self.client.post(_decide_urls(self.held)["approve"], form_keyed({}))

        self.held.refresh_from_db()
        self.assertEqual(self.held.state, ApprovalRequest.State.APPROVED)

    def test_a_viewer_reads_the_request_and_who_decides_it_with_no_decision_to_press(self):
        self.assertEqual(self._offered(self.viewer), set())

        self.client.force_login(self.viewer)
        queue = text(self.client.get(reverse("action_items")))
        self.assertIn(ASKED, queue)
        self.assertIn("operators", queue)

    def test_a_viewer_s_forged_decision_is_still_refused(self):
        self.client.force_login(self.viewer)

        for url in _decide_urls(self.held).values():
            self.client.post(url, form_keyed({}))

        self.held.refresh_from_db()
        self.assertEqual(self.held.state, ApprovalRequest.State.PENDING)

    def test_a_request_whose_agent_was_paused_offers_only_reject_which_still_answers_it(self):
        set_agents_paused(AgentAccessCommand(paused=True), principal=web_principal(self.operator))

        self.assertEqual(self._offered(self.operator), {"reject"})

        self.client.force_login(self.operator)
        self.client.post(_decide_urls(self.held)["approve"], form_keyed({}))
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, ApprovalRequest.State.PENDING)
        self.client.post(_decide_urls(self.held)["reject"], form_keyed({}))
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, ApprovalRequest.State.REJECTED)

    def test_every_door_offers_what_the_rule_allows_each_reader(self):
        from hq.platform.application.approvals import DECISIONS, refusals

        paths = dict(DECISIONS)
        for name, user in (("operator", self.operator), ("viewer", self.viewer)):
            for paused in (False, True):
                with self.subTest(reader=name, paused=paused):
                    set_agents_paused(AgentAccessCommand(paused=paused), principal=web_principal(self.operator))
                    allowed = refusals(web_principal(user), (self.held,))[self.held.pk]

                    self.assertEqual(
                        self._offered(user), {paths[name] for name, refused in allowed.items() if refused is None}
                    )


@tag("source", "tree")
class DecisionLinkSourceTests(SimpleTestCase):
    def test_only_the_rule_makes_a_decision_s_address(self):
        """A decision's button is made only by ``approvals.offers``, which
        asks the rule first, and addressed by the command it runs: no code or
        template names its route."""

        names = {
            pattern.name
            for pattern in get_resolver("hq.domains.control_plane.urls").url_patterns
            if isinstance(pattern, URLPattern) and getattr(pattern.callback, "view_class", None) is ApprovalDecisionView
        }
        self.assertTrue(names)
        # A route is named in quotes, alone or after its namespace.
        named = re.compile("|".join(rf"[\"':]{re.escape(name)}[\"']" for name in names))
        sources = [
            path
            for path in (*ROOT.glob("hq/**/*.py"), *ROOT.glob("demo/**/*.py"), *ROOT.glob("templates/**/*.html"))
            if "tests" not in path.relative_to(ROOT).parts
        ]
        naming = sorted(
            path.relative_to(ROOT).as_posix() for path in sources if named.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(naming, ["hq/domains/control_plane/urls.py"], "approvals.offers")
