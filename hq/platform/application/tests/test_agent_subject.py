"""One client two people signed in is two agents: each person's rule, block,
retry keys, allowance and held asks are their own, and the Agents page names
each. Every test here sets the state through the product's own path, so each
fails where HQ keys an agent by its client alone.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from hq.domains.control_plane.models import ApprovalRequest
from hq.domains.projects.models import Project
from hq.platform.application.tests.markup import select
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AgentIdentity

from .. import agent_admission, agent_registry, roles
from ..agent_access import SET_AGENT_BLOCK, refusal
from ..agent_activity import standings
from ..capabilities import capability_registry, execute_capability
from ..capability_policy import Rule, columns, decide, set_rule
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..security import web_principal
from ..tokens import token_principal
from .test_approvals import POLICY_KEY, declare_policy, policy_document, update_payload

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
