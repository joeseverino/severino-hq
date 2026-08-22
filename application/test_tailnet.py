"""Reachability answers, and the three of them there are.

HQ does not evaluate the policy -- Tailscale does, during the sweep, and what
is stored is which principals a rule admits. So these fix the reading of that
answer: who counts as asking, what counts as an answer, and what counts as not
knowing. The last is the one worth having tests for, because a gap in the sweep
reported as "not allowed" is a lie that looks exactly like the truth.

Every pair on the operator's own tailnet is currently allowed -- one owner, one
admin group -- so a refusal cannot be observed there and is built here.
"""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from control_plane.models import ProviderInventory

from .tailnet import may_reach, proposed_grant


def a_tailnet(*devices):
    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={"records": list(devices), "observed_at": timezone.now()},
    )


def a_device(name, *, user="", tags=(), reach=()):
    return {
        "name": name,
        "user": user,
        "tags": list(tags),
        "reach": [
            {"port": port, "who": list(who), "rules": [{"who": list(who), "to": [], "line": 1}]}
            for port, who in reach
        ],
    }


class AllowedTests(TestCase):
    def test_a_device_admitted_by_its_user_is_allowed(self):
        a_tailnet(
            a_device("a-laptop", user="someone@example.test"),
            a_device("a-server", reach=[(443, ["someone@example.test"])]),
        )

        self.assertTrue(may_reach("a-laptop", "a-server", 443).allowed)

    def test_a_device_admitted_by_a_tag_it_carries_is_allowed(self):
        a_tailnet(
            a_device("a-laptop", user="someone@example.test", tags=["tag:office"]),
            a_device("a-server", reach=[(443, ["tag:office"])]),
        )

        verdict = may_reach("a-laptop", "a-server", 443)

        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.via, ("tag:office",))

    def test_the_rule_that_decided_it_comes_back_with_the_answer(self):
        """An answer nobody can trace to a rule has to be taken on faith."""

        a_tailnet(
            a_device("a-laptop", user="someone@example.test"),
            a_device("a-server", reach=[(443, ["someone@example.test"])]),
        )

        self.assertEqual(len(may_reach("a-laptop", "a-server", 443).rules), 1)


class RefusedTests(TestCase):
    def setUp(self):
        a_tailnet(
            a_device("a-laptop", user="someone@example.test", tags=["tag:office"]),
            a_device("a-server", tags=["tag:server"], reach=[(22, ["tag:admin"])]),
        )

    def test_a_device_no_rule_names_is_refused(self):
        verdict = may_reach("a-laptop", "a-server", 22)

        self.assertTrue(verdict.known)
        self.assertFalse(verdict.allowed)

    def test_the_refusal_says_who_it_is_open_to_instead(self):
        self.assertIn("tag:admin", may_reach("a-laptop", "a-server", 22).detail)

    def test_it_offers_the_grant_that_would_allow_it(self):
        """Nobody reads "not allowed" and stops there."""

        self.assertEqual(
            proposed_grant("a-laptop", "a-server", 22),
            {"src": ["tag:office"], "dst": ["tag:server"], "ip": ["tcp:22"]},
        )

    def test_the_proposal_names_principals_rather_than_addresses(self):
        """A grant naming an address works once, then the address moves."""

        proposal = proposed_grant("a-laptop", "a-server", 22)

        self.assertTrue(proposal["src"][0].startswith("tag:"))
        self.assertTrue(proposal["dst"][0].startswith("tag:"))


class CannotSayTests(TestCase):
    """A gap in the sweep is not a decision the policy made."""

    def setUp(self):
        a_tailnet(
            a_device("a-laptop", user="someone@example.test"),
            a_device("a-server", reach=[(443, ["someone@example.test"])]),
        )

    def test_a_port_nobody_asked_about_is_not_reported_as_refused(self):
        verdict = may_reach("a-laptop", "a-server", 9999)

        self.assertFalse(verdict.known)
        self.assertEqual(verdict.label, "Cannot say")

    def test_a_device_the_sweep_never_saw_is_not_reported_as_refused(self):
        verdict = may_reach("a-ghost", "a-server", 443)

        self.assertFalse(verdict.known)
        self.assertIn("a-ghost", verdict.detail)

    def test_a_device_carrying_no_identity_says_why(self):
        """No user and no tag means no rule can name it, which is not a refusal."""

        a_tailnet(
            a_device("a-nameless"),
            a_device("a-server", reach=[(443, ["someone@example.test"])]),
        )

        verdict = may_reach("a-nameless", "a-server", 443)

        self.assertFalse(verdict.known)
        self.assertIn("no user or tag", verdict.detail)

    def test_nothing_is_proposed_for_a_question_that_has_no_answer(self):
        self.assertEqual(proposed_grant("a-ghost", "a-server", 443), {})
