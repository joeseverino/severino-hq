"""A call the token's grant alone refuses is ``insufficient_scope``, naming the
permission to grant the client, on both doors; what a new sign-in cannot fix
stays ``forbidden`` or ``denied_by_policy``.

The identity provider is the ceiling, so a refusal it caused says what to tick
there. One HQ caused (the person's role, ``HELD_ONLY_AT``, an operator's rule)
names nothing to grant, since granting it would change nothing.
"""

import json
import re

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from mcp import types

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.application import roles, tokens
from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
from hq.platform.application.security import subject_of
from hq.platform.core.test_runner import no_default_role
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.server import mcp

from .tokens import ISSUER, RESOURCE, _serving, _token

PERSON = "3f1c2b7e-5a4d-4e8b-9c1a-0d2e3f4a5b6c"
AGENT = "example-agent"
PROJECT = {"name": "Scoped", "slug": "scoped", "status": "active"}
PAUSE = {"paused": True}


def _challenge(header):
    """A ``WWW-Authenticate`` value's scheme and its parameters, by name."""

    scheme, _, rest = header.partition(" ")
    return scheme, dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', rest))


@override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, SEVERINO_API_LEEWAY_SECONDS=30, OIDC_RP_SIGN_ALGO="RS256"
)
class ScopeRefusalTests(TestCase):
    def setUp(self):
        with no_default_role():
            self.person = get_user_model().objects.create_user("example-person")
        bind_sign_in(self.person, sign_in_subject(ISSUER, PERSON))
        roles.assign(self.person, roles.Role.OPERATOR)

    def _api(self, name, payload, scope):
        with _serving():
            return self.client.post(
                f"/api/v2/capabilities/{name}/",
                data=json.dumps({"payload": payload}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(AGENT, sub=PERSON, scope=scope)}",
                HTTP_IDEMPOTENCY_KEY="example-scope",
            )

    def _mcp(self, name, payload, scope):
        principal = tokens.token_principal({"client_id": AGENT, "sub": PERSON, "scope": scope}, "mcp")
        params = types.CallToolRequestParams(
            name="execute_capability",
            arguments={"name": name, "payload": {**payload, "idempotency_key": "example-scope"}},
        )
        with set_principal(principal):
            return async_to_sync(mcp._call)(None, params)

    def _both(self, name, payload, scope):
        """The API's response and the MCP tool's result for one call by a token granting ``scope``."""

        return self._api(name, payload, scope), self._mcp(name, payload, scope)

    def test_a_permission_the_person_holds_and_the_token_lacks_is_named_to_grant(self):
        api, over_mcp = self._both("project.create", PROJECT, "read")

        self.assertEqual(api.status_code, 403, api.content)
        error = api.json()["error"]
        self.assertEqual((error["code"], error["details"]["scope"]), ("insufficient_scope", "write_projects"))
        self.assertIn("Pocket ID", error["message"])
        scheme, challenge = _challenge(api.headers["WWW-Authenticate"])
        self.assertEqual(scheme, "Bearer")
        self.assertEqual((challenge["error"], challenge["scope"]), ("insufficient_scope", "write_projects"))
        # A tool error, so the client stays connected for what it may call.
        self.assertTrue(over_mcp.is_error)
        self.assertEqual(over_mcp.structured_content["error"]["code"], "insufficient_scope")
        self.assertEqual(over_mcp.structured_content["error"]["details"], {"scope": "write_projects"})

    def test_what_a_new_sign_in_cannot_fix_names_nothing_to_grant(self):
        cases = (
            ("the person's role lacks it", roles.Role.VIEWER, "project.create", PROJECT, "forbidden"),
            # A token naming the permission is still refused it: only a person holds it.
            ("held only by a person", roles.Role.OPERATOR, "agent.access.set", PAUSE, "forbidden"),
            ("an operator's rule", roles.Role.OPERATOR, "project.create", PROJECT, "denied_by_policy"),
        )
        for case, role, name, payload, code in cases:
            with self.subTest(case):
                roles.assign(self.person, role)
                CapabilityRule.objects.all().delete()
                if code == "denied_by_policy":
                    CapabilityRule.objects.create(
                        subject=subject_of(AGENT, sign_in_subject(ISSUER, PERSON)),
                        capability=name,
                        rule=CapabilityRule.Rule.DENY,
                    )

                api, over_mcp = self._both(name, payload, "read write_projects govern_agents")

                self.assertEqual(api.status_code, 403, api.content)
                self.assertEqual(api.json()["error"]["code"], code)
                self.assertNotIn("details", api.json()["error"])
                self.assertNotIn("WWW-Authenticate", api.headers)
                self.assertTrue(over_mcp.is_error)
                self.assertEqual(over_mcp.structured_content["error"]["code"], code)
                self.assertNotIn("details", over_mcp.structured_content["error"])

    def test_a_machines_own_token_is_named_what_to_grant_its_client(self):
        """A client-credentials token has no person to bound it, so its client's grant is the whole ceiling."""

        with _serving():
            api = self.client.post(
                "/api/v2/capabilities/project.create/",
                data=json.dumps({"payload": PROJECT}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(AGENT, scope='read')}",
                HTTP_IDEMPOTENCY_KEY="example-scope",
            )

        self.assertEqual(api.status_code, 403, api.content)
        self.assertEqual(api.json()["error"]["details"], {"scope": "write_projects"})

    def test_a_read_the_token_cannot_make_names_the_read_to_grant(self):
        """A list a door offers by some entry, rather than by one permission, names the baseline read."""

        scope = "write_projects"
        with _serving():
            api = self.client.get(
                "/api/v2/resources/projects/",
                HTTP_AUTHORIZATION=f"Bearer {_token(AGENT, sub=PERSON, scope=scope)}",
            )
        principal = tokens.token_principal({"client_id": AGENT, "sub": PERSON, "scope": scope}, "mcp")
        params = types.CallToolRequestParams(name="list_resource", arguments={"name": "projects"})
        with set_principal(principal):
            over_mcp = async_to_sync(mcp._call)(None, params)

        self.assertEqual(api.status_code, 403, api.content)
        self.assertEqual(api.json()["error"]["details"], {"scope": "read"})
        self.assertTrue(over_mcp.is_error)
        self.assertEqual(over_mcp.structured_content["error"]["details"], {"scope": "read"})
