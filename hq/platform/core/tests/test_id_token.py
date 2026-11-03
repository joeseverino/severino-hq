"""A browser's ID token is checked as the machine API checks a token, against the keys HQ holds."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
from django.core.exceptions import SuspiciousOperation
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from hq.platform.api.tests.tokens import _KEY, ISSUER, KID, OTHER_KEY, _held, _serving
from hq.platform.application import tokens
from hq.platform.core.models import AuditLog
from hq.platform.core.oidc import SSO_FAILURE_SESSION_KEY, HQOIDCAuthenticationBackend

CLIENT = "example-hq"
NONCE = "example-nonce"


def _id_token(key=_KEY, typ="JWT", **overrides) -> str:
    now = datetime.now(tz=UTC)
    claims = {
        "iss": ISSUER,
        "aud": CLIENT,
        "sub": "example-person",
        "nonce": NONCE,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    claims = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID, "typ": typ})


def _never_fetched():
    """The library's own read of the key set, which no sign-in may reach."""

    return patch("mozilla_django_oidc.auth.requests.get", side_effect=AssertionError("fetched the key set"))


@override_settings(OIDC_ISSUER=ISSUER, OIDC_RP_CLIENT_ID=CLIENT, OIDC_RP_SIGN_ALGO="RS256", SEVERINO_API_RESOURCE="")
class IdTokenTests(SimpleTestCase):
    def _verify(self, token):
        with _serving(), _never_fetched():
            return HQOIDCAuthenticationBackend().verify_token(token, nonce=NONCE)

    def test_an_id_token_for_this_client_verifies_from_the_held_keys(self):
        self.assertEqual(self._verify(_id_token())["sub"], "example-person")

    def test_every_check_refuses_its_token(self):
        past = datetime.now(tz=UTC) - timedelta(seconds=10)
        for case, token in (
            ("another client", _id_token(aud="example-other")),
            ("another issuer", _id_token(iss="https://evil.example.test")),
            ("another key", _id_token(key=OTHER_KEY)),
            # The browser keeps the library's leeway, none, unlike the machine API's.
            ("expired", _id_token(exp=past)),
            ("no expiry", _id_token(exp=None)),
            ("no issuer", _id_token(iss=None)),
            ("an access token", _id_token(typ="at+jwt")),
            ("a logout token", _id_token(typ="logout+jwt")),
            ("another party among several", _id_token(aud=[CLIENT, "example-other"], azp="example-other")),
            ("no party among several", _id_token(aud=[CLIENT, "example-other"])),
        ):
            with self.subTest(case), self.assertRaises(tokens.TokenError):
                self._verify(token)

    def test_a_token_for_several_parties_verifies_when_this_client_is_the_authorized_one(self):
        claims = self._verify(_id_token(aud=[CLIENT, "example-other"], azp=CLIENT))

        self.assertEqual(claims["azp"], CLIENT)

    def test_another_algorithm_is_refused(self):
        token = jwt.encode(
            {"iss": ISSUER, "aud": CLIENT, "nonce": NONCE, "exp": datetime.now(tz=UTC) + timedelta(minutes=5)},
            _KEY,
            algorithm="RS512",
            headers={"kid": KID, "typ": "JWT"},
        )
        with self.assertRaises(tokens.TokenError):
            self._verify(token)

    def test_the_library_still_checks_the_nonce(self):
        with _serving(), self.assertRaises(SuspiciousOperation):
            HQOIDCAuthenticationBackend().verify_token(_id_token(), nonce="another-nonce")

    @override_settings(OIDC_ISSUER="")
    def test_no_configured_issuer_refuses_every_token(self):
        with self.assertRaises(tokens.TokenError):
            self._verify(_id_token(iss=""))

    @override_settings(OIDC_RP_CLIENT_ID="")
    def test_no_configured_client_refuses_every_token(self):
        with self.assertRaises(tokens.TokenError):
            self._verify(_id_token(aud=""))

    def test_a_key_not_held_asks_for_a_read_and_never_fetches(self):
        tokens.KEYS.wanted.clear()
        try:
            for held in ({}, {"another-key": _held(_KEY)[KID]}):
                with (
                    self.subTest(held=list(held)),
                    patch.object(tokens.KEYS, "held", held),
                    _never_fetched(),
                    self.assertLogs("severino.api", "WARNING"),
                    self.assertRaises(tokens.TokenError),
                ):
                    HQOIDCAuthenticationBackend().verify_token(_id_token(), nonce=NONCE)
                self.assertTrue(tokens.KEYS.wanted.is_set())
                tokens.KEYS.wanted.clear()
        finally:
            tokens.KEYS.wanted.clear()

    @override_settings(OIDC_OP_JWKS_ENDPOINT=f"{ISSUER}/.well-known/jwks.json")
    def test_the_keys_are_kept_for_sign_in_without_a_machine_api(self):
        with (
            patch.object(tokens.KEYS, "held", {}),
            patch.object(jwt.PyJWKClient, "get_signing_keys", lambda client: list(_held(_KEY).values())),
        ):
            with tokens.keeping_keys():
                pass
            with _never_fetched():
                claims = HQOIDCAuthenticationBackend().verify_token(_id_token(), nonce=NONCE)
        self.assertEqual(claims["sub"], "example-person")


@override_settings(OIDC_ISSUER=ISSUER, OIDC_RP_CLIENT_ID=CLIENT, OIDC_RP_SIGN_ALGO="RS256")
class RefusedSignInTests(TestCase):
    def test_a_refused_id_token_is_a_failed_sign_in_with_its_reason(self):
        request = RequestFactory().get("/oidc/callback/", {"code": "example-code", "state": "example-state"})
        request.session = {}
        backend = HQOIDCAuthenticationBackend()

        with (
            _serving(),
            _never_fetched(),
            patch.object(
                HQOIDCAuthenticationBackend,
                "get_token",
                return_value={"id_token": _id_token(aud="example-other"), "access_token": "example-access"},
            ),
            patch.object(HQOIDCAuthenticationBackend, "get_userinfo") as userinfo,
        ):
            self.assertIsNone(backend.authenticate(request, nonce=NONCE))

        userinfo.assert_not_called()
        self.assertIn(SSO_FAILURE_SESSION_KEY, request.session)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.DENIED).exists())
