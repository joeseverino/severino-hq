"""Sign out ends the identity provider's session as well as HQ's, from the ID token the sign-in kept."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from hq.platform.api.tests.tokens import ISSUER, _serving
from hq.platform.core.oidc import HQOIDCAuthenticationBackend
from hq.platform.core.tests.test_id_token import CLIENT, NONCE, _id_token

END_SESSION = f"{ISSUER}/api/oidc/end-session"
CLAIMS = {
    "sub": "example-person",
    "preferred_username": "example-person",
    "email": "person@example.com",
    "email_verified": True,
    "groups": ["admins"],
}


@override_settings(
    OIDC_ISSUER=ISSUER,
    OIDC_RP_CLIENT_ID=CLIENT,
    OIDC_RP_SIGN_ALGO="RS256",
    OIDC_OP_LOGOUT_ENDPOINT=END_SESSION,
    SEVERINO_OIDC_ROLE_GROUPS={"admins": "operator"},
)
class SignOutTests(TestCase):
    def _signed_in_through_the_provider(self) -> str:
        """Sign the test client in through the provider's callback, and return the ID token it was given."""

        id_token = _id_token()
        session = self.client.session
        request = RequestFactory().get("/oidc/callback/", {"code": "example-code", "state": "example-state"})
        request.session = session
        with (
            _serving(),
            patch.object(
                HQOIDCAuthenticationBackend,
                "get_token",
                return_value={"id_token": id_token, "access_token": "example-access"},
            ),
            patch.object(HQOIDCAuthenticationBackend, "get_userinfo", return_value=CLAIMS),
        ):
            user = HQOIDCAuthenticationBackend().authenticate(request, nonce=NONCE)
        self.assertIsNotNone(user)
        session.save()
        self.client.force_login(user, backend="hq.platform.core.oidc.HQOIDCAuthenticationBackend")
        return id_token

    def test_a_provider_session_is_sent_to_end_its_session_there(self):
        id_token = self._signed_in_through_the_provider()

        response = self.client.post(reverse("logout"))

        self.assertEqual(response.status_code, 302)
        target = urlsplit(response["Location"])
        self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}", END_SESSION)
        self.assertEqual(
            parse_qs(target.query),
            {
                "id_token_hint": [id_token],
                "client_id": [CLIENT],
                "post_logout_redirect_uri": [f"http://testserver{settings.LOGOUT_REDIRECT_URL}"],
            },
        )
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_a_session_without_an_id_token_is_signed_out_of_hq_alone(self):
        self.client.force_login(get_user_model().objects.create_user("example-person", password="x" * 20))

        response = self.client.post(reverse("logout"))

        self.assertRedirects(response, settings.LOGOUT_REDIRECT_URL, fetch_redirect_response=False)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_a_get_signs_nobody_out(self):
        self._signed_in_through_the_provider()

        response = self.client.get(reverse("logout"))

        self.assertEqual(response.status_code, 405)
        self.assertIn(SESSION_KEY, self.client.session)
