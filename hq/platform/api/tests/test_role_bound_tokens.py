"""A token a person signed in never does more than that person's role, through either door.

A delegated agent holds its grant within the agent ceiling and the person's
role; a person's own device holds its grant within their role; a machine's own
token holds its grant within the ceiling; a token naming a person with no role
is refused.
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from hq.platform.application import roles, tokens
from hq.platform.application.agent_admission import admit
from hq.platform.application.agent_registry import observe
from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
from hq.platform.application.security import AuthorizationError, Capability, DevicePrincipal, agent_ceiling
from hq.platform.core.models import AgentIdentity, AuditLog, OwnDevice
from hq.platform.core.test_runner import no_default_role

from .tokens import ISSUER, RESOURCE, _serving, _token

PERSON = "3f1c2b7e-5a4d-4e8b-9c1a-0d2e3f4a5b6c"
AGENT = "example-agent"
PHONE = "example-phone"
# What a generous token asks for: a read, a record write and an infrastructure change.
ASKED = "read write_projects manage_infrastructure"


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_ENABLE_WRITES=True,
)
class RoleBoundTokenTests(TestCase):
    def setUp(self):
        with no_default_role():
            self.person = get_user_model().objects.create_user("example-person")
        bind_sign_in(self.person, sign_in_subject(ISSUER, PERSON))

    def _hold(self, role):
        roles.assign(self.person, role)

    def _principal(self, scope, *, client=AGENT, sub=PERSON, interface="api"):
        return tokens.token_principal({"client_id": client, "sub": sub, "scope": scope}, interface)

    def test_a_delegated_agent_holds_its_grant_within_the_ceiling_and_its_persons_role(self):
        for role, writes in ((roles.Role.OPERATOR, True), (roles.Role.VIEWER, False)):
            self._hold(role)
            for interface in ("api", "mcp"):
                with self.subTest(role=role, interface=interface):
                    principal = self._principal(ASKED, interface=interface)

                    self.assertEqual(principal.actor, AGENT)
                    self.assertTrue(principal.permits(Capability.READ))
                    self.assertEqual(principal.permits("write_projects"), writes)
                    # Outside the ceiling, whatever the person may do.
                    self.assertFalse(principal.permits(Capability.MANAGE_INFRASTRUCTURE))
                    self.assertEqual(principal.granted, frozenset(ASKED.split()))

    def test_a_persons_device_holds_its_grant_within_their_role(self):
        OwnDevice.objects.create(user=self.person, client_id=PHONE)
        for role, changes in ((roles.Role.OPERATOR, True), (roles.Role.VIEWER, False)):
            self._hold(role)
            with self.subTest(role=role):
                principal = self._principal(f"{ASKED} own_device", client=PHONE)

                self.assertIsInstance(principal, DevicePrincipal)
                self.assertTrue(principal.permits(Capability.READ))
                self.assertEqual(principal.permits(Capability.MANAGE_INFRASTRUCTURE), changes)

    def test_a_machines_own_token_holds_its_grant_within_the_ceiling(self):
        principal = self._principal(ASKED, sub=f"client-{AGENT}")

        self.assertEqual(principal.capabilities, frozenset(ASKED.split()) & agent_ceiling())

    def test_a_token_naming_a_person_with_no_role_is_refused(self):
        OwnDevice.objects.create(user=self.person, client_id=PHONE)
        for case, client, sub in (
            ("a person with no role", AGENT, PERSON),
            ("their device", PHONE, PERSON),
            ("a person HQ has never seen", AGENT, "example-stranger"),
        ):
            scope = f"{ASKED} own_device" if client == PHONE else ASKED
            with self.subTest(case), self.assertRaises(AuthorizationError):
                self._principal(scope, client=client, sub=sub)

    def test_the_door_answers_403_for_a_person_with_no_role(self):
        with _serving():
            response = self.client.get(
                "/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token(AGENT, sub=PERSON, scope='read')}"
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "insufficient_scope")

    def test_a_viewers_agent_cannot_run_a_write(self):
        self._hold(roles.Role.VIEWER)
        token = _token(AGENT, sub=PERSON, scope="read write_projects")
        with _serving():
            response = self.client.post(
                "/api/v2/capabilities/project.create/",
                data={"payload": {"name": "Viewer", "slug": "viewer", "status": "active"}},
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                HTTP_IDEMPOTENCY_KEY="example-viewer-write",
            )

        self.assertEqual(response.status_code, 403, response.content)

    def test_the_agents_person_is_recorded_and_a_change_is_audited(self):
        self._hold(roles.Role.OPERATOR)
        observe(self._principal("read"))
        self.assertEqual(AgentIdentity.objects.get(client_id=AGENT).person, self.person)

        observe(self._principal("read", sub=f"client-{AGENT}"))
        self.assertIsNone(AgentIdentity.objects.get(client_id=AGENT).person)
        changed = AuditLog.objects.get(action=AuditLog.Action.UPDATED, metadata__actor=AGENT)
        self.assertEqual((changed.metadata["person"], changed.metadata["was"]), ("", self.person.get_username()))

    def test_one_admission_reads_a_bounded_number_of_rows_whatever_the_role_holds(self):
        self._hold(roles.Role.OPERATOR)
        token = _token(AGENT, sub=PERSON, scope=ASKED)
        with _serving():
            admit(token, interface="api", source="")
            with CaptureQueriesContext(connection) as queries:
                admitted = admit(token, interface="api", source="")

        self.assertEqual(admitted.actor, AGENT)
        self.assertLessEqual(len(queries), 6, [query["sql"] for query in queries])
