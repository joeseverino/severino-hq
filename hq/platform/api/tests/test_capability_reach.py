"""The API's capability catalog lists only what a token could ever hold."""

from django.test import TestCase, override_settings

from .test_api import ISSUER, RESOURCE, _serving, _token


@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, OIDC_RP_SIGN_ALGO="RS256")
class CatalogReachTests(TestCase):
    def test_a_token_is_told_of_what_it_lacks_and_not_of_what_only_a_person_holds(self):
        with _serving():
            response = self.client.get("/api/v2/capabilities/", HTTP_AUTHORIZATION=f"Bearer {_token()}")

        self.assertEqual(response.status_code, 200)
        specs = {spec["name"]: spec for spec in response.json()["data"]["capabilities"]}
        self.assertFalse(specs["project.create"]["permitted"])
        self.assertFalse({"certificate.upload", "receipt.upload"} & set(specs))
