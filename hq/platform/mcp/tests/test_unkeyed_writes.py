"""An agent's change over MCP without a retry key is refused on every capability.

Walked from the registry, so a capability added later is held to it the day it
lands: a retry of a keyless change would act twice. The machine API's header is
required by its document, which the property suite holds.
"""

from django.test import TestCase, override_settings

from hq.platform.api.tests.tokens import ISSUER, RESOURCE
from hq.platform.application import tokens
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.security import AuthorizationError, Capability
from hq.platform.mcp import services
from hq.platform.mcp.identity import set_principal


def _scope(spec) -> str:
    named = {Capability.READ.value}
    named.update(item.value if isinstance(item, Capability) else item for item in spec.required_capabilities)
    return " ".join(sorted(named))


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class UnkeyedWriteTests(TestCase):
    def _runnable(self, interface):
        """Every change an agent on ``interface`` may run, with the token granting it."""

        for spec in capability_registry():
            if spec.effect == "read":
                continue
            scope = _scope(spec)
            claims = {"client_id": "example-agent", "sub": "example-agent", "scope": scope}
            principal = tokens.token_principal(claims, interface)
            try:
                spec.authorize(principal)
            except AuthorizationError:
                # Refused for authority first, which no key changes.
                continue
            yield spec.name, spec, scope, principal

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
