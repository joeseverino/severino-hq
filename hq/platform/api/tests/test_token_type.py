"""Only an RFC 9068 access token verifies, and an audience list naming HQ's resource is HQ's."""

from django.test import SimpleTestCase, override_settings

from hq.platform.application import tokens

from .tokens import _KEY, ISSUER, KID, RESOURCE, _serving, _token


def _typed(typ, **claims):
    """A token as ``_token`` makes it, with its header's ``typ`` set to ``typ``, or none."""

    import jwt

    header = {"kid": KID} if typ is None else {"kid": KID, "typ": typ}
    unsigned = jwt.decode(_token(**claims), options={"verify_signature": False})
    return jwt.encode(unsigned, _KEY, algorithm="RS256", headers=header)


@override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, SEVERINO_API_LEEWAY_SECONDS=30, OIDC_RP_SIGN_ALGO="RS256"
)
class AccessTokenTypeTests(SimpleTestCase):
    def test_an_access_token_verifies_by_either_spelling_of_its_type(self):
        for typ in ("at+jwt", "application/at+jwt", "AT+JWT"):
            with self.subTest(typ=typ), _serving():
                self.assertEqual(tokens.verify(_typed(typ))["client_id"], "example-automation")

    def test_any_other_token_the_provider_signs_is_refused(self):
        """An ID token is signed by the same key, and its audience could be made to match."""

        for typ in (None, "JWT", "id_token+jwt", "logout+jwt"):
            with self.subTest(typ=typ), _serving(), self.assertRaises(tokens.TokenError):
                tokens.verify(_typed(typ))

    def test_an_audience_list_holding_the_resource_verifies(self):
        """The provider adds its own issuer beside the resource when an identity scope was granted."""

        with _serving():
            claims = tokens.verify(_token(aud=[RESOURCE, ISSUER]))

        self.assertEqual(claims["client_id"], "example-automation")

    def test_an_audience_list_without_the_resource_is_refused(self):
        with _serving(), self.assertRaises(tokens.TokenError):
            tokens.verify(_token(aud=[ISSUER, "https://elsewhere.example.test"]))
