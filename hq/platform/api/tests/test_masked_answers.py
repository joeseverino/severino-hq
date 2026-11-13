"""A field mask that cuts inside a record's nested part answers what the document describes."""

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token


@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, OIDC_RP_SIGN_ALGO="RS256")
class MaskedAnswerTests(TestCase):
    # Every response below is held to the published OpenAPI document.
    client_class = ContractClient

    def test_an_answer_cut_inside_a_nested_part_holds_to_the_document(self):
        """The contract client validates each answer against the published document."""

        ManagedResource.objects.create(
            key="example-rewrite", kind="adguard.rewrite", spec={"domain": "app.example.test", "answer": "192.0.2.10"}
        )
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(scope='read')}"}
        with _serving():
            listed = self.client.get("/api/v2/resources/infrastructure/?fields=key,health.state", **headers)
            detail = self.client.get(
                "/api/v2/resources/infrastructure/example-rewrite/?fields=resource.health.state", **headers
            )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([set(item["health"]) for item in listed.json()["data"]["items"]], [{"state"}])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(set(detail.json()["data"]), {"resource"})
        self.assertEqual(set(detail.json()["data"]["resource"]["health"]), {"state"})
