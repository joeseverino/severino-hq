"""How an agent signs in, as HQ records it: as itself, or through a person's sign-in.

Pocket ID names a client-credentials token's subject ``client-<client_id>``
and a token a person signed in for after that person, so the subject says
which, on the token each door already verified.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.platform.application.tokens import token_principal
from hq.platform.core.models import AgentIdentity

from ..agent_registry import observe
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..security import person_of

AGENT = "example-agent"
MACHINE = {"client_id": AGENT, "sub": f"client-{AGENT}", "scope": "read"}
DELEGATED = {"client_id": AGENT, "sub": "3f1c2b7e-5a4d-4e8b-9c1a-0d2e3f4a5b6c", "scope": "read"}


def _signs_in() -> str:
    return AgentIdentity.objects.get(client_id=AGENT).signs_in


class SignInTests(TestCase):
    def setUp(self):
        # The person the delegated token names has signed in to HQ.
        self.person = person = get_user_model().objects.create_user("example-person")
        bind_sign_in(person, sign_in_subject(settings.OIDC_ISSUER, DELEGATED["sub"]))

    def test_a_client_credentials_token_signs_in_as_itself(self):
        observe(token_principal(MACHINE, "mcp"))

        self.assertEqual(_signs_in(), "machine")

    def test_a_token_a_person_signed_in_for_signs_in_through_them(self):
        observe(token_principal(DELEGATED, "api"))

        self.assertEqual(_signs_in(), "person")

    def test_an_issuer_that_names_no_client_signs_in_as_itself(self):
        observe(token_principal({"sub": AGENT, "scope": "read"}, "api"))

        self.assertEqual(_signs_in(), "machine")

    def test_a_client_that_changes_how_it_signs_in_is_recorded_as_it_is_now(self):
        observe(token_principal(MACHINE, "mcp"))
        observe(token_principal(DELEGATED, "mcp"))

        self.assertEqual(_signs_in(), "person")

    def test_signing_in_through_a_person_makes_it_theirs(self):
        self.assertEqual(person_of(token_principal(DELEGATED, "mcp")), self.person)

    def test_signing_in_as_itself_makes_it_nobodys(self):
        self.assertIsNone(person_of(token_principal(MACHINE, "mcp")))
