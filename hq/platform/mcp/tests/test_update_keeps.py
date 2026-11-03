"""Over MCP, an update sets the fields it was sent and keeps the rest, and the
capability's schema shows an update's fields with no default."""

from decimal import Decimal

from django.test import TestCase

from hq.domains.assets.models import Asset
from hq.domains.projects.models import Project
from hq.platform.application.security import Principal

from .. import services
from ..identity import set_principal

WRITER = Principal("example-writer", "mcp", frozenset({"write_projects"}))
ASSET_WRITER = Principal("example-writer", "mcp", frozenset({"write_assets"}))


class AnUpdateOverMCPKeepsTests(TestCase):
    def setUp(self):
        self.enterContext(set_principal(WRITER))

    def test_an_update_sending_one_field_keeps_another_that_has_a_default(self):
        Project.objects.create(name="HQ", slug="example-hq", status=Project.Status.ACTIVE, notes="Kept.")

        result = services.execute_capability(
            "project.update", {"name": "Renamed", "idempotency_key": "example-update-keeps"}, target="example-hq"
        )

        self.assertTrue(result["ok"], result)
        project = Project.objects.get(slug="example-hq")
        self.assertEqual((project.name, project.status, project.notes), ("Renamed", Project.Status.ACTIVE, "Kept."))

    def test_an_asset_update_sent_only_its_cost_needs_no_name_and_changes_only_its_cost(self):
        Asset.objects.create(item_name="Example box", slug="example-box", notes="Kept.", business_use_percentage=40)

        with set_principal(ASSET_WRITER):
            result = services.execute_capability(
                "asset.update", {"total_cost": "12.00", "idempotency_key": "example-cost-only"}, target="example-box"
            )

        self.assertTrue(result["ok"], result)
        asset = Asset.objects.get(slug="example-box")
        self.assertEqual(
            (asset.total_cost, asset.item_name, asset.notes, asset.business_use_percentage),
            (Decimal("12.00"), "Example box", "Kept.", 40),
        )

    def test_an_updates_schema_requires_nothing_and_shows_no_default(self):
        described = services.describe_capabilities(name="project.update")
        (spec,) = described["capabilities"]

        properties = spec["input_schema"]["properties"]
        self.assertEqual([name for name, field in properties.items() if "default" in field], [])
        self.assertNotIn("required", spec["input_schema"])
