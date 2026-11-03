"""Over the machine API, an update sets the fields it was sent and keeps the
rest, and the document says so: an update's fields show no default."""

import json
from decimal import Decimal

from django.test import SimpleTestCase, TestCase, override_settings

from hq.domains.assets.models import Asset
from hq.domains.projects.models import Project

from .. import openapi
from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

KEPT = "A field left out keeps its value."


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_ENABLE_WRITES=True,
)
class AnUpdateOverTheAPIKeepsTests(TestCase):
    # Every response is also held to the OpenAPI document.
    client_class = ContractClient

    def test_an_update_sending_one_field_keeps_another_that_has_a_default(self):
        Project.objects.create(name="HQ", slug="example-hq", status=Project.Status.ACTIVE, notes="Kept.")

        with _serving():
            response = self.client.post(
                "/api/v2/capabilities/project.update/",
                data=json.dumps({"payload": {"name": "Renamed"}, "target": "example-hq"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}",
                HTTP_IDEMPOTENCY_KEY="example-update-keeps",
            )

        self.assertEqual(response.status_code, 200, response.content)
        project = Project.objects.get(slug="example-hq")
        self.assertEqual((project.name, project.status, project.notes), ("Renamed", Project.Status.ACTIVE, "Kept."))

    def test_an_asset_update_sent_only_its_cost_needs_no_name_and_changes_only_its_cost(self):
        Asset.objects.create(item_name="Example box", slug="example-box", notes="Kept.", business_use_percentage=40)

        with _serving():
            response = self.client.post(
                "/api/v2/capabilities/asset.update/",
                data=json.dumps({"payload": {"total_cost": "12.00"}, "target": "example-box"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_assets')}",
                HTTP_IDEMPOTENCY_KEY="example-cost-only",
            )

        self.assertEqual(response.status_code, 200, response.content)
        asset = Asset.objects.get(slug="example-box")
        self.assertEqual(
            (asset.total_cost, asset.item_name, asset.notes, asset.business_use_percentage),
            (Decimal("12.00"), "Example box", "Kept.", 40),
        )


class TheDocumentSaysAnUpdateKeepsTests(SimpleTestCase):
    def test_an_updates_fields_show_no_default_and_its_summary_says_why(self):
        document = openapi.document()
        operation = document["paths"]["/api/v2/capabilities/project.update/"]["post"]
        body = document["components"]["schemas"]["ProjectUpdateRequest"]["properties"]["payload"]

        self.assertIn(KEPT, operation["description"] + operation.get("summary", ""))
        self.assertEqual([name for name, field in body["properties"].items() if "default" in field], [])
        self.assertNotIn("required", body)
        schemas = document["components"]["schemas"]
        created = schemas["ProjectCreateRequest"]["properties"]["payload"]
        # A command several capabilities take alike is the component named for its type.
        created = schemas[created["$ref"].rsplit("/", 1)[-1]] if "$ref" in created else created
        self.assertIn("default", created["properties"]["status"])
