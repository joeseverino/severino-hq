"""Over the machine API, a document HQ refuses record by record is a 400 whose details are its problems."""

import json

from django.test import TestCase, override_settings

from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.projects.models import Project

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_ENABLE_WRITES=True,
)
class ProblemRefusalTests(TestCase):
    # Every response is also held to the OpenAPI document.
    client_class = ContractClient

    def post(self, name, payload, scope):
        with _serving():
            return self.client.post(
                f"/api/v2/capabilities/{name}/",
                data=json.dumps({"payload": payload}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope=scope)}",
                HTTP_IDEMPOTENCY_KEY=f"key-{name}",
            )

    def test_an_import_with_a_bad_record_is_400_with_each_problem(self):
        response = self.post(
            "hq.import",
            {"projects": [{"slug": "example-good", "name": "Good"}, {"name": "No slug"}]},
            "write_projects write_assets",
        )

        self.assertEqual(response.status_code, 400, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_input")
        self.assertEqual([problem["loc"] for problem in error["details"]], [["projects", 1, "slug"]])
        self.assertFalse(Project.objects.exists())

    def test_a_docs_sync_with_a_bad_record_is_400_with_each_problem(self):
        response = self.post(
            "documentation.sync",
            {"manifest": [{"title": "No ID"}]},
            "sync_documentation",
        )

        self.assertEqual(response.status_code, 400, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_input")
        self.assertEqual(error["details"], [{"doc_id": "", "errors": ["missing doc_id"]}])
        self.assertFalse(DocumentationRecord.objects.exists())
