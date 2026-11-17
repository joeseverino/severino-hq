"""Each refusal the API answers carries a code its operation lists for that status,
and the contract client fails a test that receives one it does not."""

from unittest import mock

from django.test import TestCase, override_settings
from jsonschema import ValidationError

from hq.platform.application.security import AuthorizationError, PolicyDenied
from hq.platform.application.tests.host_only import host_only

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class RefusalCodeTests(TestCase):
    client_class = ContractClient

    def _capabilities(self, raised):
        with (
            _serving(),
            host_only(),
            mock.patch("hq.platform.api.views.describe_capabilities", side_effect=raised),
        ):
            return self.client.get("/api/v2/capabilities/", HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}")

    def test_a_code_the_operation_lists_at_its_status_passes(self):
        self.assertEqual(self._capabilities(AuthorizationError("Refused.")).status_code, 403)

    def test_a_code_the_operation_does_not_list_at_its_status_fails_the_test(self):
        # A policy refusal is a 403 the capability catalog never answers.
        with self.assertRaises(ValidationError):
            self._capabilities(PolicyDenied("Refused."))
