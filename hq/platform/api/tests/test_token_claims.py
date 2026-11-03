"""An access token is held to the algorithm, issuer, audience and claims it is pinned to, with HQ's leeway."""

from datetime import UTC, datetime, timedelta

import jwt
from django.test import SimpleTestCase, override_settings

from hq.platform.application import tokens

from .tokens import _KEY, ISSUER, KID, RESOURCE, _serving, _token


@override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, SEVERINO_API_LEEWAY_SECONDS=30, OIDC_RP_SIGN_ALGO="RS256"
)
class AccessTokenClaimTests(SimpleTestCase):
    def test_a_token_just_expired_within_the_leeway_verifies(self):
        with _serving():
            claims = tokens.verify(_token(exp=datetime.now(tz=UTC) - timedelta(seconds=10)))

        self.assertEqual(claims["client_id"], "example-automation")

    def test_a_token_naming_only_its_client_is_that_client_as_itself(self):
        """A test's token is a person's delegated grant only where it names the person."""
        with _serving():
            own = tokens.verify(_token("example-agent"))
            delegated = tokens.verify(_token("example-agent", sub="example-person"))

        self.assertEqual((own["client_id"], own["sub"]), ("example-agent", "example-agent"))
        self.assertEqual((delegated["client_id"], delegated["sub"]), ("example-agent", "example-person"))
        with self.assertRaises(TypeError):
            _token(client_id="example-agent")

    def test_a_token_without_its_issuer_or_audience_is_refused(self):
        for claim in ("iss", "aud"):
            unsigned = jwt.decode(_token(), options={"verify_signature": False})
            del unsigned[claim]
            token = jwt.encode(unsigned, _KEY, algorithm="RS256", headers={"kid": KID, "typ": "at+jwt"})
            with self.subTest(claim), _serving(), self.assertRaises(tokens.TokenError):
                tokens.verify(token)

    def test_another_algorithm_is_refused(self):
        unsigned = jwt.decode(_token(), options={"verify_signature": False})
        token = jwt.encode(unsigned, _KEY, algorithm="RS512", headers={"kid": KID, "typ": "at+jwt"})

        with _serving(), self.assertRaises(tokens.TokenError):
            tokens.verify(token)

    @override_settings(OIDC_ISSUER="")
    def test_no_configured_issuer_refuses_every_token(self):
        with _serving(), self.assertRaises(tokens.TokenError):
            tokens.verify(_token(iss=""))

    def test_a_malformed_token_is_refused(self):
        with self.assertRaises(tokens.TokenError):
            tokens.verify("not-a-token")


class GrantClaimTests(SimpleTestCase):
    def test_a_grant_is_read_from_the_claims_pocket_id_writes(self):
        for name in ("scope", "scp"):
            with self.subTest(name):
                self.assertEqual(tokens.granted({name: "example.write read"}), frozenset({"example.write", "read"}))

    def test_a_claim_pocket_id_never_writes_grants_nothing(self):
        self.assertEqual(tokens.granted({"permissions": ["example.write"]}), frozenset())
