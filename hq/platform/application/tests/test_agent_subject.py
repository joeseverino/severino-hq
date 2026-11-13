"""One client two people signed in is two agents: each person's rule, block,
retry keys, allowance and held asks are their own, and the Agents page names
each. Every test here sets the state through the product's own path, so each
fails where HQ keys an agent by its client alone.
"""

from dataclasses import replace
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from mcp.shared.exceptions import MCPError

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.domains.projects.models import Project
from hq.platform.application.tests.markup import select
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AgentIdentity
from hq.platform.mcp import tasks

from .. import agent_admission, agent_registry, roles
from ..agent_access import SET_AGENT_BLOCK, refusal
from ..agent_activity import standings
from ..approvals import ApprovalError, ApproveCommand, approve, asked_by, pending
from ..capabilities import capability_registry, execute_capability
from ..capability_policy import Rule, columns, decide, set_rule
from ..controller import claim_next_operation
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..resources import list_resource
from ..security import web_principal
from ..tokens import token_principal
from .test_approvals import GATED_KIND, POLICY_KEY, declare_policy, policy_document, reconcile_payload, update_payload

CLIENT = "example-assistant"
SCOPE = "read write_projects delete_projects manage_infrastructure"
ANNA, BEN = "example-anna-subject", "example-ben-subject"


def _signed_in(subject: str):
    return token_principal({"client_id": CLIENT, "sub": subject, "scope": SCOPE}, "mcp")


class OneSubjectPerAgentAndPersonTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.anna = users.objects.create_user("anna", password="x" * 20)
        self.ben = users.objects.create_user("ben", password="x" * 20)
        for user, subject in ((self.anna, ANNA), (self.ben, BEN)):
            roles.assign(user, roles.Role.OPERATOR)
            bind_sign_in(user, sign_in_subject(settings.OIDC_ISSUER, subject))
        self.as_anna, self.as_ben = _signed_in(ANNA), _signed_in(BEN)
        agent_registry.observe(self.as_anna)
        agent_registry.observe(self.as_ben)

    def column_of(self, user):
        (column,) = [column for column in columns() if column.identity.person_id == user.pk]
        return column

    def test_each_person_who_signed_the_client_in_is_a_row_of_its_own(self):
        rows = AgentIdentity.objects.filter(client_id=CLIENT)

        self.assertEqual(sorted(row.person.get_username() for row in rows), ["anna", "ben"])
        self.assertEqual(len({standing.subject for standing in standings()}), 2)

    def test_annas_on_rule_consents_for_anna_alone(self):
        deleting = capability_registry()["project.delete"]
        set_rule(subject=self.column_of(self.anna).subject, spec=deleting, rule=Rule.ALLOW, user=self.anna)

        self.assertEqual(decide(deleting, self.as_anna, {}, None).rule, Rule.ALLOW)
        self.assertEqual(decide(deleting, self.as_ben, {}, None).rule, Rule.APPROVE)

    def test_bens_retry_of_annas_key_runs_as_ben_rather_than_replaying_annas_answer(self):
        payload = {"name": "Shared", "idempotency_key": "example-key"}

        first = execute_capability("project.create", payload, principal=self.as_anna)
        second = execute_capability("project.create", payload, principal=self.as_ben)

        self.assertTrue(first["ok"] and second["ok"], (first, second))
        self.assertEqual(Project.objects.filter(name="Shared").count(), 2)

    @override_settings(SEVERINO_AGENT_CALLS_AT_ONCE=1)
    def test_annas_calls_in_flight_leave_bens_allowance_whole(self):
        with agent_admission.at_once(self.as_anna) as first, agent_admission.at_once(self.as_anna) as refused:
            self.assertIsNone(first)
            self.assertIsNotNone(refused)
            with agent_admission.at_once(self.as_ben) as ben:
                self.assertIsNone(ben)

    def test_bens_identical_ask_is_held_as_his_own_rather_than_as_annas(self):
        declare_policy()
        payload = update_payload(policy_document("group:elsewhere"))

        anna = execute_capability(
            "infrastructure.resource.update", keyed(payload), principal=self.as_anna, target=POLICY_KEY
        )
        ben = execute_capability(
            "infrastructure.resource.update", keyed(payload), principal=self.as_ben, target=POLICY_KEY
        )

        self.assertEqual(
            (anna.get("status"), ben.get("status")), ("awaiting_approval", "awaiting_approval"), (anna, ben)
        )
        self.assertNotEqual(anna["approval"]["id"], ben["approval"]["id"])
        self.assertEqual(ApprovalRequest.objects.count(), 2)

    def test_a_block_on_annas_row_leaves_ben_calling(self):
        answer = execute_capability(
            SET_AGENT_BLOCK,
            keyed({"blocked": True}),
            principal=web_principal(self.anna),
            target=self.column_of(self.anna).subject,
        )
        self.assertTrue(answer["ok"], answer)

        self.assertEqual(refusal(self.as_anna).code, "agent_blocked")
        self.assertIsNone(refusal(self.as_ben))

    def test_the_agents_page_names_each_person_on_the_clients_own_row(self):
        self.client.force_login(self.anna)

        page = self.client.get(reverse("agent_policy"))

        rows = [row for row in select(page, "data-agent") if row.attrs.get("data-client") == CLIENT]
        self.assertEqual(len(rows), 2)
        named = [row.words for row in rows]
        self.assertTrue(any("For anna" in words for words in named), named)
        self.assertTrue(any("For ben" in words for words in named), named)

    def hold(self, principal):
        answer = execute_capability(
            "infrastructure.resource.reconcile", reconcile_payload(), principal=principal, target=POLICY_KEY
        )
        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        return answer["approval"]["id"]

    def block_anna(self, *, blocked=True):
        answer = execute_capability(
            SET_AGENT_BLOCK,
            keyed({"blocked": blocked}),
            principal=web_principal(self.anna),
            target=self.column_of(self.anna).subject,
        )
        self.assertTrue(answer["ok"], answer)

    def claim(self):
        return claim_next_operation("controller-example", capabilities=((GATED_KIND, "reconcile"),))["operation"]

    def test_a_person_and_a_machine_read_only_their_own_holds_on_a_shared_client(self):
        declare_policy()
        machine = replace(self.as_ben, person_key="")
        callers = (self.as_anna, self.as_ben, machine)
        holds = [(caller, self.hold(caller)) for caller in callers]

        for caller, own_id in holds:
            with self.subTest(person=caller.person_key):
                self.assertEqual({str(held.pk) for held in pending(principal=caller)}, {own_id})
                for owner, held_id in holds:
                    self.assertEqual(asked_by(held_id, principal=caller) is not None, owner == caller)

    def test_the_action_queue_leaves_out_another_persons_hold(self):
        declare_policy()
        held_id = self.hold(self.as_anna)
        path = reverse("core:approval_entry", kwargs={"approval_id": held_id})

        def paths(principal):
            return {
                urlsplit(item["url"]).path for item in list_resource("action_items", None, principal=principal)["items"]
            }

        self.assertIn(path, paths(self.as_anna))
        self.assertNotIn(path, paths(self.as_ben))

    def test_tasks_refuse_another_person_for_pending_and_settled_holds(self):
        declare_policy()
        held_id = self.hold(self.as_anna)
        for settled in (False, True):
            if settled:
                approve(ApproveCommand(held_id), principal=web_principal(self.anna))
            self.assertEqual(tasks.state(held_id, self.as_anna)[0]["taskId"], held_id)
            for read in (tasks.state, tasks.acknowledge, tasks.cancel):
                with self.subTest(settled=settled, method=read.__name__), self.assertRaises(MCPError):
                    read(held_id, self.as_ben)

    def test_annas_block_leaves_bens_hold_approvable_and_holds_her_own(self):
        declare_policy()
        anna_id, ben_id = self.hold(self.as_anna), self.hold(self.as_ben)
        self.block_anna()

        approve(ApproveCommand(ben_id), principal=web_principal(self.ben))
        with self.assertRaises(ApprovalError):
            approve(ApproveCommand(anna_id), principal=web_principal(self.anna))
        self.assertEqual(OperationRequest.objects.get().requested_person_key, self.as_ben.person_key)

    def test_annas_block_leaves_bens_work_claimable(self):
        declare_policy()
        approve(ApproveCommand(self.hold(self.as_ben)), principal=web_principal(self.ben))
        self.block_anna()

        self.assertIsNotNone(self.claim())

    def test_annas_work_stays_queued_until_her_own_block_lifts(self):
        declare_policy()
        approve(ApproveCommand(self.hold(self.as_anna)), principal=web_principal(self.anna))
        self.block_anna()

        self.assertIsNone(self.claim())
        self.block_anna(blocked=False)
        self.assertIsNotNone(self.claim())

    def test_a_machine_on_the_same_client_is_not_held_by_a_persons_block(self):
        declare_policy()
        approve(ApproveCommand(self.hold(replace(self.as_ben, person_key=""))), principal=web_principal(self.ben))
        self.assertEqual(OperationRequest.objects.get().requested_person_key, "")
        self.block_anna()

        self.assertIsNotNone(self.claim())

    def test_a_legacy_operation_with_unknown_person_keeps_the_clients_brake(self):
        declare_policy()
        approve(ApproveCommand(self.hold(self.as_ben)), principal=web_principal(self.ben))
        OperationRequest.objects.update(requested_person_key=None)
        self.block_anna()

        self.assertIsNone(self.claim())
        self.block_anna(blocked=False)
        self.assertIsNotNone(self.claim())
