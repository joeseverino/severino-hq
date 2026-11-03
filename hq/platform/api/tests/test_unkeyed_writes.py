"""An agent's change without a retry key is refused on every capability, at both doors an agent calls.

Walked from the registry, so a capability added later is held to it the day it
lands: a retry of a keyless change would act twice.
"""

import json

from django.test import TestCase, override_settings

from hq.platform.application import tokens
from hq.platform.application.capabilities import authorize_capability, capability_registry
from hq.platform.application.security import AuthorizationError, Capability
from hq.platform.mcp import services
from hq.platform.mcp.identity import set_principal

from .tokens import ISSUER, RESOURCE, _serving, _token

EVERY_AGENT_SWITCH = {
    f"SEVERINO_AGENT_ENABLE_{switch}": True
    for switch in ("WRITES", "DOC_SYNC", "INFRASTRUCTURE", "PRUNE", "DELETES", "CERT_RENEWAL", "LOOKUP", "CALENDAR")
}


def _scope(spec) -> str:
    named = {Capability.READ.value}
    named.update(item.value if isinstance(item, Capability) else item for item in spec.required_capabilities)
    return " ".join(sorted(named))


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    **EVERY_AGENT_SWITCH,
)
class UnkeyedWriteTests(TestCase):
    def _runnable(self, interface):
        """Every change an agent on ``interface`` may run, with the token granting it."""

        for name, spec in sorted(capability_registry().items()):
            if spec.effect == "read":
                continue
            scope = _scope(spec)
            claims = {"client_id": "example-agent", "sub": "example-agent", "scope": scope}
            principal = tokens.token_principal(claims, interface)
            try:
                authorize_capability(spec, principal)
            except AuthorizationError:
                # Refused for authority first, which no key changes.
                continue
            yield name, spec, scope, principal

    def test_every_change_through_the_api_without_its_header_is_refused(self):
        walked = 0
        with _serving():
            for name, spec, scope, _principal in self._runnable("api"):
                # A target of the capability's kind, so only the missing key is wrong.
                target = "1" if spec.target_kind == "integer" else "example"
                body = {"payload": {}, **({"target": target} if spec.target_kind else {})}
                response = self.client.post(
                    f"/api/v2/capabilities/{name}/",
                    data=json.dumps(body),
                    content_type="application/json",
                    HTTP_AUTHORIZATION=f"Bearer {_token('example-agent', scope=scope)}",
                )
                with self.subTest(capability=name):
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")
                walked += 1
        self.assertTrue(walked)

    def test_every_change_through_mcp_without_a_key_is_refused(self):
        walked = 0
        for name, spec, _scope_text, principal in self._runnable("mcp"):
            # A target of the capability's kind, so only the missing key is wrong.
            target = ("1" if spec.target_kind == "integer" else "example") if spec.target_kind else None
            with set_principal(principal):
                result = services.execute_capability(name, {}, target=target)
            with self.subTest(capability=name):
                self.assertEqual(result["error"]["code"], "idempotency_key_required")
            walked += 1
        self.assertTrue(walked)
