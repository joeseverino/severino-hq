"""An agent a person signed in keeps that person's own settings, pins and set-asides.

Its token names the person in ``sub``, so what it keeps is theirs, recorded
under the agent's client. A machine's own token names nobody, so it is refused
them whatever Pocket ID granted it, and the catalog never offers them to it.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import ActionItemRead, Appearance, AuditLog

from ..action_items import RESTORE, SET_ASIDE
from ..appearance import SET_THEME
from ..approvals import ApproveCommand, approve
from ..calendar_entries import calendar_choices
from ..calendar_specs import CHOOSE_SOURCE
from ..capabilities import capability_registry, describe_capabilities, execute_capability
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..pins import CHOOSE, MOVE, PIN, SERVICE, ordered
from ..security import web_principal
from ..tokens import token_principal

AGENT = "example-agent"
PERSON = "3f1c2b7e-5a4d-4e8b-9c1a-0d2e3f4a5b6c"
# What keeps something of a person's, written out: the decision, so a change to it is a change here.
PERSONAL = "set_preferences pin_favorites set_aside_action_items"
# What only a person in the browser, or HQ itself, may do, whatever a token is granted.
HELD_ONLY = ("decide_approvals", "govern_agents", "upload_certificate", "upload_receipt", "end_sessions")
ROW = {"example:widgets": "r1"}
ONE, TWO = "one.example.test", "two.example.test"


def _signed_in(interface: str = "mcp", scope: str = f"read {PERSONAL}"):
    return token_principal({"client_id": AGENT, "sub": PERSON, "scope": scope}, interface)


def _machine(interface: str = "mcp"):
    return token_principal({"client_id": AGENT, "sub": f"client-{AGENT}", "scope": f"read {PERSONAL}"}, interface)


def _run(name, payload, principal):
    return execute_capability(name, keyed(payload), principal=principal)


class PersonBoundAgentTests(TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user("example-person")
        bind_sign_in(self.person, sign_in_subject(settings.OIDC_ISSUER, PERSON))

    def test_it_keeps_its_persons_own_settings_pins_and_set_asides(self):
        for interface in ("mcp", "api"):
            with self.subTest(interface=interface):
                agent = _signed_in(interface)
                answers = (
                    _run(SET_THEME, {"theme": "dark"}, agent),
                    _run(CHOOSE_SOURCE, {"source": "history.deploys", "shown": True}, agent),
                    _run(PIN, {"kind": SERVICE, "key": ONE}, agent),
                    _run(PIN, {"kind": SERVICE, "key": TWO}, agent),
                    _run(MOVE, {"kind": SERVICE, "key": TWO, "up": True}, agent),
                )
                for answer in answers:
                    self.assertTrue(answer.get("ok"), answer)

                self.assertEqual(Appearance.objects.get(user=self.person).theme, "dark")
                self.assertEqual(calendar_choices(self.person), {"history.deploys": True})
                self.assertEqual(list(ordered(self.person, SERVICE)), [TWO, ONE])

                self.assertTrue(_run(CHOOSE, {"kind": SERVICE, "keys": [ONE]}, agent).get("ok"))
                self.assertEqual(list(ordered(self.person, SERVICE)), [ONE])

                self.assertTrue(_run(SET_ASIDE, {"rows": ROW}, agent).get("ok"))
                self.assertTrue(ActionItemRead.objects.filter(user=self.person, key="example:widgets").exists())
                self.assertTrue(_run(RESTORE, {"keys": list(ROW)}, agent).get("ok"))
                self.assertFalse(ActionItemRead.objects.exists())

                _run(CHOOSE, {"kind": SERVICE, "keys": []}, agent)
        # Each write is the person's, and says the agent made it.
        for operation in (SET_THEME, PIN, MOVE, CHOOSE, SET_ASIDE, RESTORE):
            with self.subTest(operation=operation):
                self.assertTrue(
                    AuditLog.objects.filter(
                        user=self.person, metadata__operation=operation, metadata__actor=AGENT
                    ).exists()
                )

    def test_a_machines_token_is_refused_them_and_never_offered_them(self):
        personal = [
            name
            for name, spec in capability_registry().items()
            if set(PERSONAL.split()) & {str(needed) for needed in spec.required_capabilities}
        ]
        self.assertTrue(personal)
        for interface in ("mcp", "api"):
            machine = _machine(interface)
            listed = {
                item["name"]: item["permitted"] for item in describe_capabilities(principal=machine)["capabilities"]
            }
            for name in personal:
                with self.subTest(interface=interface, capability=name):
                    self.assertFalse(listed.get(name, False))
                    self.assertFalse(machine.permits(*capability_registry()[name].required_capabilities))
        answer = _run(PIN, {"kind": SERVICE, "key": ONE}, _machine())

        self.assertEqual(answer["error"]["code"], "forbidden")
        self.assertEqual(list(ordered(self.person, SERVICE)), [])

    def test_a_held_pin_once_approved_lands_as_its_person(self):
        CapabilityRule.objects.create(subject=AGENT, capability=PIN, rule=CapabilityRule.Rule.APPROVE)
        approver = get_user_model().objects.create_user("example-approver")

        held = _run(PIN, {"kind": SERVICE, "key": ONE}, _signed_in())
        self.assertEqual(held.get("status"), "awaiting_approval", held)
        self.assertEqual(list(ordered(self.person, SERVICE)), [])

        approve(ApproveCommand(str(held["approval"]["id"])), principal=web_principal(approver))

        self.assertEqual(ApprovalRequest.objects.get().state, ApprovalRequest.State.APPROVED)
        self.assertEqual(list(ordered(self.person, SERVICE)), [ONE])
        self.assertEqual(list(ordered(approver, SERVICE)), [])

    def test_what_only_a_person_in_the_browser_or_hq_does_stays_out_of_its_reach(self):
        agent = _signed_in(scope=" ".join(HELD_ONLY))
        for capability in HELD_ONLY:
            with self.subTest(capability=capability):
                self.assertFalse(agent.permits(capability))
