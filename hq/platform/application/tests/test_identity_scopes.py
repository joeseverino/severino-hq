"""An identity scope on a delegated token says who signed in, never what HQ allows.

A person's sign-in through an agent's client asks Pocket ID for ``openid``
and ``offline_access`` beside HQ's own permissions, and the token carries
them all in one grant.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.tokens import token_principal
from hq.platform.core.models import AgentIdentity

from ..agent_registry import observe
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..security import AuthorizationError

AGENT = "example-agent"
PERSON = "3f1c2b7e-5a4d-4e8b-9c1a-0d2e3f4a5b6c"


def _delegated(scope: str) -> dict[str, str]:
    return {"client_id": AGENT, "sub": PERSON, "scope": scope}


class IdentityScopeTests(TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user("example-person", password="x" * 20)
        bind_sign_in(self.person, sign_in_subject(settings.OIDC_ISSUER, PERSON))

    def test_a_token_granting_only_identity_scopes_is_refused(self):
        with self.assertRaisesMessage(AuthorizationError, "This token grants no HQ permissions"):
            token_principal(_delegated("openid offline_access"), "mcp")

    def test_a_token_is_granted_only_its_hq_permissions(self):
        principal = token_principal(_delegated("openid offline_access read"), "mcp")

        self.assertEqual(principal.granted, frozenset({"read"}))
        self.assertEqual(principal.capabilities, frozenset({"read"}))

    def test_the_agents_page_counts_only_hq_permissions(self):
        observe(token_principal(_delegated("openid offline_access read"), "mcp"))
        self.client.force_login(self.person)

        response = self.client.get(reverse("agent_policy"))

        (standing,) = response.context["standings"]
        # The page reads "{in force} of {granted}" from these two.
        self.assertEqual(AgentIdentity.objects.get(client_id=AGENT).granted, ["read"])
        self.assertEqual(standing.in_force, ("read",))
