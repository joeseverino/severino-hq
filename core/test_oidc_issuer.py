"""The ID token's issuer is checked against a configured one, and none refuses all."""

from __future__ import annotations

from unittest.mock import patch

from django.core.exceptions import SuspiciousOperation
from django.test import SimpleTestCase, override_settings
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from core.oidc import HQOIDCAuthenticationBackend


@override_settings(OIDC_RP_CLIENT_ID="hq")
class IssuerTests(SimpleTestCase):
    def _verify(self, iss):
        backend = HQOIDCAuthenticationBackend.__new__(HQOIDCAuthenticationBackend)
        with patch.object(
            OIDCAuthenticationBackend,
            "verify_token",
            return_value={"aud": "hq", "iss": iss},
        ):
            return backend.verify_token("token")

    @override_settings(OIDC_ISSUER="https://sso.example.com")
    def test_the_configured_issuer_is_accepted(self):
        self.assertEqual(self._verify("https://sso.example.com")["iss"], "https://sso.example.com")

    @override_settings(OIDC_ISSUER="https://sso.example.com")
    def test_another_issuer_is_refused(self):
        with self.assertRaises(SuspiciousOperation):
            self._verify("https://other.example.com")

    @override_settings(OIDC_ISSUER="")
    def test_no_configured_issuer_refuses_every_token(self):
        with self.assertRaises(SuspiciousOperation):
            self._verify("https://sso.example.com")
