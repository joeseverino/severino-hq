"""A held change belongs to the caller that asked it.

Two callers asking the same change hold two requests, each read and approved
as its own asker; the change still applies once, because the second approval
finds what it was measured against already moved. A credential reads only its
own held requests, wherever they are listed to it; a person reads every one.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest

from ..approvals import ApprovalError, ApproveCommand, approve, asked, asked_by, pending
from ..capabilities import execute_capability
from ..resources import list_resource
from ..security import Capability, Principal, cli_principal
from .test_approvals import POLICY_KEY, an_operator, declare_policy, policy_document, reconcile_payload, update_payload
from .writers import keyed

GRANT = frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE})
FIRST = Principal("example-agent", "mcp", GRANT)
SECOND = Principal("example-other-agent", "mcp", GRANT)


def ask_reconcile(principal: Principal) -> dict:
    return execute_capability(
        "infrastructure.resource.reconcile", reconcile_payload(), principal=principal, target=POLICY_KEY
    )


def ask_update(principal: Principal) -> dict:
    return execute_capability(
        "infrastructure.resource.update",
        keyed(update_payload(policy_document("group:asked-by-both"))),
        principal=principal,
        target=POLICY_KEY,
    )


@override_settings(SEVERINO_AGENT_ENABLE_INFRASTRUCTURE=True)
class HoldsBelongToTheirAskerTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_policy()

    def test_two_callers_asking_the_same_change_each_hold_their_own(self):
        first = ask_reconcile(FIRST)
        second = ask_reconcile(SECOND)

        self.assertNotEqual(first["approval"]["id"], second["approval"]["id"])
        self.assertFalse(second["repeated"])
        self.assertEqual(second["approval"]["requested_actor"], SECOND.actor)
        self.assertIsNotNone(asked_by(second["approval"]["id"], principal=SECOND))
        # Each caller's own retry is still the one request it already holds.
        again = ask_reconcile(SECOND)
        self.assertEqual(again["approval"]["id"], second["approval"]["id"])
        self.assertTrue(again["repeated"])

    def test_approving_the_second_callers_request_runs_as_that_caller(self):
        ask_reconcile(FIRST)
        second = ask_reconcile(SECOND)

        approve(ApproveCommand(second["approval"]["id"]), principal=an_operator())

        operation = OperationRequest.objects.get()
        self.assertEqual(operation.requested_actor, SECOND.actor)
        self.assertEqual(operation.requested_interface, SECOND.interface)

    def test_the_same_change_approved_for_both_callers_applies_once(self):
        first = ask_update(FIRST)
        second = ask_update(SECOND)
        operator = an_operator()

        approve(ApproveCommand(first["approval"]["id"]), principal=operator)
        with self.assertRaises(ApprovalError):
            approve(ApproveCommand(second["approval"]["id"]), principal=operator)

        states = dict(ApprovalRequest.objects.values_list("requested_actor", "state"))
        self.assertEqual(
            states, {FIRST.actor: ApprovalRequest.State.APPROVED, SECOND.actor: ApprovalRequest.State.STALE}
        )

    def test_a_same_caller_ask_written_between_the_read_and_the_write_answers_the_existing_request(self):
        written = []

        def the_other_ask_commits_first(held, *args, **kwargs):
            # The identical ask from the same caller, landing after this one
            # looked and found nothing, and before it inserts.
            if not written:
                twin = ApprovalRequest(
                    **{
                        field.attname: getattr(held, field.attname)
                        for field in ApprovalRequest._meta.concrete_fields
                        if not field.primary_key
                    }
                )
                twin.save(force_insert=True)
                written.append(twin)
            return asked(held, *args, **kwargs)

        with patch("hq.platform.application.approvals.asked", side_effect=the_other_ask_commits_first):
            answered = ask_reconcile(FIRST)

        self.assertTrue(answered["ok"])
        self.assertTrue(answered["repeated"])
        self.assertEqual(answered["approval"]["id"], str(written[0].pk))
        self.assertEqual(ApprovalRequest.objects.count(), 1)

    def test_a_caller_reads_only_its_own_held_requests_and_a_person_reads_every_one(self):
        ask_reconcile(FIRST)
        ask_reconcile(SECOND)

        self.assertEqual({held.requested_actor for held in pending(principal=SECOND)}, {SECOND.actor})
        for person in (an_operator(), cli_principal()):
            with self.subTest(person=person.interface):
                self.assertEqual(
                    {held.requested_actor for held in pending(principal=person)}, {FIRST.actor, SECOND.actor}
                )

    def test_the_action_queue_carries_only_the_callers_own_held_requests(self):
        mine = ask_reconcile(SECOND)["approval"]
        theirs = ask_reconcile(FIRST)["approval"]

        def urls(principal):
            return {item["url"] for item in list_resource("action_items", None, principal=principal)["items"]}

        read = urls(SECOND)
        self.assertIn(mine["review_url"], read)
        self.assertNotIn(theirs["review_url"], read)
        self.assertLessEqual({mine["review_url"], theirs["review_url"]}, urls(an_operator()))
