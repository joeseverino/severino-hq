"""Reachability answers, and the three of them there are.

HQ does not evaluate the policy: Tailscale does, during the sweep, and what
is stored is which principals a rule admits. So these fix the reading of that
answer: who counts as asking, what counts as an answer, and what counts as not
knowing. The last is the one worth having tests for, because a gap in the sweep
reported as "not allowed" is a lie that looks exactly like the truth.

Every pair on the operator's own tailnet is currently allowed (one owner, one
admin group) so a refusal cannot be observed there and is built here.
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


class AliasPrincipalTests(TestCase):
    """A policy may admit a device by an alias it gives that device's address.

    Naming a machine rather than whoever is signed in on it is the stricter
    thing to say, and it is how this tailnet's admin reach is written. Counted
    as only a user and some tags, every such grant reads as no grant at all.
    """

    def _device(self, **overrides):
        from application.tailnet import Device

        return Device(
            **{
                "name": "a-laptop",
                "user": "someone@example",
                "addresses": ("100.64.0.9",),
                **overrides,
            }
        )

    def test_an_alias_is_a_name_a_rule_can_admit_by(self):
        self.assertEqual(
            self._device(aliases=("laptop", "laptop-v6")).principals,
            frozenset({"someone@example", "laptop", "laptop-v6"}),
        )

    def test_a_device_the_policy_names_is_admitted_by_that_name(self):
        from application.tailnet import Verdict, may_reach

        known = {
            "a-laptop": self._device(aliases=("laptop",)),
            "a-server": self._device(
                name="a-server",
                user="",
                addresses=("100.64.0.10",),
                reach={443: ("laptop",)},
            ),
        }

        verdict = may_reach("a-laptop", "a-server", 443, known)

        self.assertIsInstance(verdict, Verdict)
        self.assertTrue(verdict.allowed, verdict.detail)
        self.assertEqual(verdict.via, ("laptop",))

    def test_without_the_alias_the_same_grant_reads_as_a_refusal(self):
        """Without the alias, the same grant reads as a refusal."""

        from application.tailnet import may_reach

        known = {
            "a-laptop": self._device(),
            "a-server": self._device(
                name="a-server",
                user="",
                addresses=("100.64.0.10",),
                reach={443: ("laptop",)},
            ),
        }

        self.assertFalse(may_reach("a-laptop", "a-server", 443, known).allowed)


class SpokenAsDevicesTests(TestCase):
    """A person reads devices, not the policy's per-address aliases."""

    def _known(self):
        from application.tailnet import Device

        return {
            "a-laptop": Device(name="a-laptop", addresses=("100.64.0.9",),
                               aliases=("laptop", "laptop-v6")),
            "a-server": Device(name="a-server", addresses=("100.64.0.10",),
                               aliases=("server", "server-v6")),
        }

    def test_both_aliases_of_a_device_are_that_device_once(self):
        from application.tailnet import alias_owners, as_devices

        owners = alias_owners(self._known())

        self.assertEqual(
            as_devices(("laptop", "laptop-v6", "group:household"), owners),
            ("a-laptop", "group:household"),
        )

    def test_destinations_become_one_port_list_per_device(self):
        from application.tailnet import alias_owners, by_device

        owners = alias_owners(self._known())

        self.assertEqual(
            by_device(("server-v6:443", "server:53", "server-v6:53", "server:22"), owners),
            ("a-server: 22, 53, 443",),
        )
        self.assertEqual(by_device(("autogroup:internet",), owners), ("autogroup:internet",))
