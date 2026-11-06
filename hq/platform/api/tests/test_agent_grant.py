"""One agent, one grant: what a token may do is the same through /mcp/ and /api/v2/.

The grant is a property of the principal, so an agent refused a write on one
surface is refused it on the other. An agent holds what its token grants,
within its person's role where a person signed it in, and nothing else.
"""

import json

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from hq.platform.application import tokens
from hq.platform.application.security import HELD_ONLY_AT, AuthorizationError, Capability, could_hold, web_principal

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

WRITE = {"payload": {"name": "Granted", "slug": "granted", "status": "active"}}
API = override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, SEVERINO_API_LEEWAY_SECONDS=30, OIDC_RP_SIGN_ALGO="RS256"
)
# Written out rather than imported, so the tests read the same at a base that
# still held these back and fail there for what it withheld.
BEYOND_A_READ = ("read_audit_log", "manage_contacts", "look_up_public_records", "write_projects")


def _mcp_principal(token):
    """The principal the MCP door hands its tools for ``token``."""

    from hq.platform.application.agent_admission import admit
    from hq.platform.application.security import MCP_INTERFACE

    return admit(token, interface=MCP_INTERFACE, source="")


@API
class OneGrantTests(TestCase):
    client_class = ContractClient

    def _write(self, token, key):
        return self.client.post(
            "/api/v2/capabilities/project.create/",
            data=json.dumps({**WRITE, "payload": {**WRITE["payload"], "slug": key}}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_a_write_the_token_does_not_grant_is_refused_on_both_surfaces(self):
        from hq.domains.projects.models import Project

        token = _token(scope="read")
        with _serving():
            on_mcp = _mcp_principal(token)
            on_api = self._write(token, "withheld")
            catalog = self.client.get("/api/v2/capabilities/?name=project.create", HTTP_AUTHORIZATION=f"Bearer {token}")

        self.assertFalse(on_mcp.permits("write_projects"))
        self.assertEqual(on_api.status_code, 403)
        self.assertEqual(on_api.json()["error"]["code"], "insufficient_scope")
        self.assertFalse(Project.objects.filter(slug="withheld").exists())
        # What the catalog offers is what the call would do.
        self.assertEqual([entry["permitted"] for entry in catalog.json()["data"]["capabilities"]], [False])

    def test_a_granted_write_runs_on_both_surfaces_with_no_setting(self):
        """The positive control: a door that refused every write would pass the test above."""

        from hq.domains.projects.models import Project

        token = _token(scope="read write_projects")
        with _serving():
            on_mcp = _mcp_principal(token)
            on_api = self._write(token, "allowed")

        self.assertTrue(on_mcp.permits("write_projects"))
        self.assertEqual(on_api.status_code, 200, on_api.content)
        self.assertTrue(Project.objects.filter(slug="allowed").exists())

    def test_a_person_signed_in_holds_their_role(self):
        user = get_user_model().objects.create_user(username="operator", password="test-only-password")
        person = web_principal(user)

        self.assertTrue(person.permits("write_projects", "delete_projects", Capability.MANAGE_INFRASTRUCTURE))


class GrantedTests(SimpleTestCase):
    """A machine's own token holds what its grant names beyond a read, with nothing set in HQ."""

    def test_each_permission_granted_is_held_on_both_surfaces(self):
        for permission in BEYOND_A_READ:
            claims = {"client_id": "example-automation", "scope": f"read {permission}"}
            for interface in ("api", "mcp"):
                with self.subTest(permission=permission, interface=interface):
                    principal = tokens.token_principal(claims, interface)
                    self.assertTrue(principal.permits(permission))

    def test_a_permission_not_granted_is_not_held(self):
        principal = tokens.token_principal({"client_id": "example-automation", "scope": "read"}, "api")

        self.assertFalse(any(principal.permits(permission) for permission in BEYOND_A_READ))

    def test_what_only_a_person_holds_stays_refused_whatever_the_grant(self):
        for permission in ("decide_approvals", "govern_agents", "end_sessions", "upload_certificate", "upload_receipt"):
            with self.subTest(permission):
                claims = {"client_id": "example-automation", "scope": f"read {permission}"}
                self.assertFalse(tokens.token_principal(claims, "mcp").permits(permission))


class TokenPrincipalTests(SimpleTestCase):
    """What every surface's token principal is, whichever surface asks."""

    SURFACES = ("api", "mcp")

    def test_a_principal_holds_exactly_what_the_token_granted(self):
        """The narrow grant is the entire point: a credential on a phone holding
        every capability would make the OAuth ceremony buy nothing."""

        claims = {"client_id": "example-agent", "scope": "read write_projects"}
        for interface in self.SURFACES:
            with self.subTest(interface):
                principal = tokens.token_principal(claims, interface)
                self.assertEqual(principal.interface, interface)
                self.assertEqual(principal.actor, "example-agent")
                self.assertTrue(principal.permits(Capability.READ, "write_projects"))
                self.assertFalse(principal.permits("delete_projects"), "nothing widens a grant")
                self.assertEqual(principal.granted, frozenset({"read", "write_projects"}))

    def test_the_whole_grant_is_kept_for_the_agents_page(self):
        claims = {"client_id": "example-agent", "scope": "read write_projects decide_approvals"}
        principal = tokens.token_principal(claims, "api")
        self.assertFalse(principal.permits("decide_approvals"))
        self.assertEqual(principal.granted, frozenset({"read", "write_projects", "decide_approvals"}))

    def test_a_token_granting_nothing_is_refused_outright(self):
        with self.assertRaises(AuthorizationError):
            tokens.token_principal({"sub": "s"}, "api")

    def test_no_token_principal_is_a_person(self):
        """Only an interface an agent reaches HQ through takes a token."""

        with self.assertRaises(ValueError):
            tokens.token_principal({"client_id": "example-agent", "scope": "read"}, "web")


# Written out rather than imported, so the tests read the same at a base
# without the device standing and fail there for what it lacks.
DEVICE = "own_device"
PERSON = "example-person"


def _own_phone(user=None):
    """``PERSON``, signed in to HQ, with example-phone registered as their own device."""

    from django.conf import settings

    from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
    from hq.platform.core.models import OwnDevice

    if user is None:
        user = get_user_model().objects.create_user(username="example-owner", password="test-only-password")
        bind_sign_in(user, sign_in_subject(settings.OIDC_ISSUER, PERSON))
    OwnDevice.objects.create(user=user, client_id="example-phone")
    return user


def _mcp_runs(principal) -> bool:
    """Whether the brake each MCP tool call asks lets ``principal`` act."""

    from hq.platform.application.agent_admission import refused

    return refused(principal) is None


@API
class DeviceStandingTests(TestCase):
    """A person's own device, marked by the device scope, stands as the person on both doors.

    Every other token is an agent: held to its grant and stopped by the pause.
    """

    client_class = ContractClient

    def setUp(self):
        from hq.platform.core.models import AgentAccess

        AgentAccess.objects.update_or_create(pk=1, defaults={"paused": True})
        _own_phone()

    def _api_write(self, token, key):
        return self.client.post(
            "/api/v2/capabilities/project.create/",
            data=json.dumps({**WRITE, "payload": {**WRITE["payload"], "slug": key}}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_a_device_is_unpaused_on_both_surfaces(self):
        from hq.domains.projects.models import Project

        token = _token("example-phone", scope=f"read write_projects {DEVICE}", sub=PERSON)
        with _serving():
            on_mcp = _mcp_principal(token)
            mcp_runs = _mcp_runs(on_mcp)
            on_api = self._api_write(token, "from-the-phone")

        self.assertTrue(on_mcp.permits("write_projects"))
        self.assertTrue(mcp_runs, "the pause never stops a device on /mcp/")
        self.assertEqual(on_api.status_code, 200, on_api.content)
        self.assertTrue(Project.objects.filter(slug="from-the-phone").exists())
        # The audit log names the device, and says it was one.
        self.assertEqual((on_mcp.actor, on_mcp.interface), ("example-phone", "device"))

    def test_a_token_without_the_scope_stays_paused(self):
        from hq.domains.projects.models import Project

        token = _token("example-agent", scope="read write_projects", sub=PERSON)
        with _serving():
            on_mcp = _mcp_principal(token)
            mcp_runs = _mcp_runs(on_mcp)
            on_api = self._api_write(token, "from-an-agent")

        self.assertEqual(on_mcp.interface, "mcp")
        self.assertFalse(mcp_runs)
        self.assertEqual((on_api.status_code, on_api.json()["error"]["code"]), (403, "agents_paused"))
        self.assertFalse(Project.objects.filter(slug="from-an-agent").exists())


@override_settings(OIDC_ISSUER=ISSUER)
class DeviceActsAsItsPersonTests(TestCase):
    """What only a person may do, a device does as that person, and the audit names the device."""

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything
        from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
        from hq.platform.application.tests.test_approvals import declare_policy

        managing_everything()
        declare_policy()
        self.user = get_user_model().objects.create_user(username="operator", password="test-only-password")
        bind_sign_in(self.user, sign_in_subject(ISSUER, PERSON))
        _own_phone(self.user)

    def _device(self, *capabilities):
        claims = {"client_id": "example-phone", "sub": PERSON, "scope": " ".join((*capabilities, DEVICE))}
        return tokens.token_principal(claims, "api")

    def test_a_device_pauses_agents_as_its_person(self):
        from hq.platform.application.capabilities import execute_capability
        from hq.platform.core.models import AgentAccess

        paused = execute_capability(
            "agent.access.set",
            {"paused": True, "idempotency_key": "example-pause"},
            principal=self._device("govern_agents"),
        )
        self.assertTrue(paused["ok"], paused)

        row = AgentAccess.objects.get(pk=1)
        self.assertTrue(row.paused)
        self.assertEqual(row.changed_by, self.user)

    def test_a_device_approves_what_an_agent_asked_for(self):
        from hq.domains.control_plane.models import ApprovalRequest
        from hq.platform.application.approvals import ApproveCommand, approve
        from hq.platform.application.capabilities import execute_capability
        from hq.platform.application.security import Principal
        from hq.platform.application.tests.test_approvals import POLICY_KEY, reconcile_payload

        agent = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
        asked = execute_capability(
            "infrastructure.resource.reconcile", reconcile_payload("asked"), principal=agent, target=POLICY_KEY
        )
        self.assertTrue(asked["ok"], asked)
        held = ApprovalRequest.objects.get(state=ApprovalRequest.State.PENDING)

        approve(ApproveCommand(str(held.id)), principal=self._device("decide_approvals", "manage_infrastructure"))

        held.refresh_from_db()
        self.assertEqual(held.state, ApprovalRequest.State.APPROVED)
        self.assertEqual(held.decided_actor, "example-phone")

    def test_a_device_whose_person_never_signed_in_keeps_nothing_of_anyones(self):
        from hq.platform.application.security import person

        claims = {"client_id": "example-phone", "sub": "example-stranger", "scope": f"pin_favorites {DEVICE}"}
        with self.assertRaises(AuthorizationError):
            person(tokens.token_principal(claims, "api"))


class DeviceScopeTests(TestCase):
    """The device scope is a standing, never a capability, held only by a person's registered client."""

    def setUp(self):
        _own_phone()

    def test_the_scope_alone_grants_no_capability(self):
        claims = {"client_id": "example-phone", "sub": PERSON, "scope": DEVICE}
        with self.assertRaises(AuthorizationError):
            tokens.token_principal(claims, "api")

    def test_a_device_holds_only_what_its_token_grants(self):
        claims = {"client_id": "example-phone", "sub": PERSON, "scope": f"read {DEVICE}"}
        principal = tokens.token_principal(claims, "mcp")

        self.assertTrue(principal.permits(Capability.READ))
        self.assertFalse(principal.permits("write_projects"))
        self.assertFalse(principal.permits(DEVICE))

    def test_a_device_holds_every_held_only_capability_its_token_grants(self):
        """A device has its person's browser authority, never narrower."""

        for capability in (held for held in HELD_ONLY_AT if could_hold("web", (held,))):
            claims = {"client_id": "example-phone", "sub": PERSON, "scope": f"{capability} {DEVICE}"}
            for surface in ("api", "mcp"):
                with self.subTest(capability=capability, surface=surface):
                    self.assertTrue(tokens.token_principal(claims, surface).permits(capability))

    def test_an_agent_with_the_same_grant_holds_none_of_them(self):
        for capability in HELD_ONLY_AT:
            claims = {"client_id": "example-agent", "sub": PERSON, "scope": str(capability)}
            for surface in ("api", "mcp"):
                with self.subTest(capability=capability, surface=surface):
                    self.assertFalse(tokens.token_principal(claims, surface).permits(capability))

    def test_a_client_known_only_by_its_metadata_document_is_never_a_device(self):
        """Pocket ID opens an API to such clients all at once, so none of them is anybody's."""

        claims = {"client_id": "https://agent.example.com/client.json", "sub": PERSON, "scope": f"read {DEVICE}"}
        with self.assertRaises(AuthorizationError):
            tokens.token_principal(claims, "mcp")

    def test_a_token_with_no_person_behind_it_is_never_a_device(self):
        for claims in (
            {"client_id": "example-phone", "sub": "client-example-phone", "scope": f"read {DEVICE}"},
            {"client_id": "example-phone", "scope": f"read {DEVICE}"},
        ):
            with self.subTest(sub=claims.get("sub")), self.assertRaises(AuthorizationError):
                tokens.token_principal(claims, "api")

    def test_no_device_principal_is_reached_through_the_web(self):
        claims = {"client_id": "example-phone", "sub": PERSON, "scope": f"read {DEVICE}"}
        with self.assertRaises(ValueError):
            tokens.token_principal(claims, "web")
