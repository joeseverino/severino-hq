from __future__ import annotations

import json
from io import StringIO
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset
from core.models import AuditLog
from hq_mcp.server import mcp
from projects.models import Project

from .documentation import sync_documentation
from .assets import AssetCommand, NotFoundError as AssetNotFoundError, save_asset
from .projects import ConflictError, ProjectCommand, save_project


class ProjectApplicationServiceTests(TestCase):
    def test_service_owns_validation_transaction_and_audit_context(self):
        result = save_project(
            ProjectCommand(name="Shared HQ", slug="shared-hq", status="active"),
            interface="mcp",
            actor="test-agent",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        event = AuditLog.objects.get(
            object_type="Project", object_id=str(Project.objects.get().pk)
        )
        self.assertEqual(
            event.metadata,
            {
                "interface": "mcp",
                "actor": "test-agent",
                "operation": "project.create",
            },
        )

        with self.assertRaises(ValidationError):
            save_project(
                ProjectCommand(
                    name="Invalid",
                    slug="invalid",
                    status="not-a-status",
                ),
                interface="mcp",
                actor="test-agent",
            )
        self.assertFalse(Project.objects.filter(slug="invalid").exists())

    def test_update_supports_optimistic_concurrency(self):
        created = save_project(
            ProjectCommand(name="HQ", slug="hq"),
            interface="cli",
            actor="operator",
        )
        project = Project.objects.get(slug="hq")
        project.name = "Changed elsewhere"
        project.save()

        with self.assertRaises(ConflictError):
            save_project(
                ProjectCommand(name="Stale write", slug="hq"),
                interface="mcp",
                actor="agent",
                current_slug="hq",
                expected_updated_at=created["project"]["updated_at"],
            )


class AdapterParityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="joe", password="test-password"
        )

    def test_web_mcp_and_cli_emit_the_same_project_shape(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("projects:create"),
            {
                "name": "Web Project",
                "slug": "web-project",
                "category": "other",
                "status": "active",
                "description": "One core",
                "technologies_used": "Django, MCP",
                "repository_url": "",
                "public_url": "",
                "deployment_notes": "",
                "security_notes": "",
                "notes": "",
            },
        )
        self.assertRedirects(
            response, reverse("projects:detail", args=["web-project"])
        )

        async def call_mcp():
            tool = mcp._tool_manager.get_tool("create_project")
            return await tool.run(
                {
                    "name": "MCP Project",
                    "slug": "mcp-project",
                    "status": "active",
                }
            )

        mcp_result = async_to_sync(call_mcp)()

        output = StringIO()
        call_command(
            "create_project",
            "cli-project",
            name="CLI Project",
            status="active",
            json=True,
            stdout=output,
        )
        cli_result = json.loads(output.getvalue())

        self.assertEqual(set(mcp_result), {"ok", "created", "project"})
        self.assertEqual(set(cli_result), {"ok", "created", "project"})
        self.assertEqual(set(mcp_result["project"]), set(cli_result["project"]))
        self.assertEqual(
            AuditLog.objects.get(object_repr="Web Project").metadata["interface"],
            "web",
        )
        self.assertEqual(
            AuditLog.objects.get(object_repr="MCP Project").metadata["interface"],
            "mcp",
        )
        self.assertEqual(
            AuditLog.objects.get(object_repr="CLI Project").metadata["interface"],
            "cli",
        )


class AssetApplicationServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Homelab", slug="homelab")

    def test_asset_service_owns_relationships_money_and_audit(self):
        result = save_asset(
            AssetCommand(
                item_name="Lab Server",
                slug="lab-server",
                total_cost=Decimal("999.99"),
                business_use_percentage=80,
                related_projects=("homelab",),
            ),
            interface="mcp",
            actor="test-agent",
        )

        self.assertEqual(result["asset"]["estimated_deductible_amount"], "799.99")
        self.assertEqual(result["asset"]["relationships"]["projects"], ["homelab"])
        event = AuditLog.objects.get(object_repr="Lab Server")
        self.assertEqual(event.metadata["operation"], "asset.create")
        self.assertEqual(event.metadata["interface"], "mcp")

    def test_missing_relationship_rolls_back_the_asset(self):
        with self.assertRaisesRegex(AssetNotFoundError, "missing-project"):
            save_asset(
                AssetCommand(
                    item_name="Invalid",
                    slug="invalid",
                    related_projects=("missing-project",),
                ),
                interface="mcp",
                actor="test-agent",
            )

        self.assertFalse(Asset.objects.filter(slug="invalid").exists())

    def test_web_mcp_and_cli_emit_the_same_asset_shape(self):
        user = get_user_model().objects.create_user(
            username="asset-operator", password="test-password"
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("assets:create"),
            {
                "item_name": "Web Asset",
                "slug": "web-asset",
                "vendor": "",
                "category": "other",
                "purchase_date": "",
                "total_cost": "10.00",
                "business_use_percentage": "100",
                "payment_method": "",
                "serial_number": "",
                "warranty_date": "",
                "status": "active",
                "notes": "",
                "related_projects": [self.project.pk],
            },
        )
        self.assertRedirects(response, reverse("assets:detail", args=["web-asset"]))

        async def call_mcp():
            tool = mcp._tool_manager.get_tool("create_asset")
            return await tool.run(
                {
                    "item_name": "MCP Asset",
                    "slug": "mcp-asset",
                    "total_cost": "20.00",
                    "related_projects": ["homelab"],
                }
            )

        mcp_result = async_to_sync(call_mcp)()

        output = StringIO()
        call_command(
            "create_asset",
            "cli-asset",
            item_name="CLI Asset",
            total_cost="30.00",
            json=True,
            stdout=output,
        )
        cli_result = json.loads(output.getvalue())

        self.assertEqual(set(mcp_result), {"ok", "created", "asset"})
        self.assertEqual(set(cli_result), {"ok", "created", "asset"})
        self.assertEqual(set(mcp_result["asset"]), set(cli_result["asset"]))
        self.assertEqual(
            AuditLog.objects.get(object_repr="Web Asset").metadata["interface"],
            "web",
        )
        self.assertEqual(
            AuditLog.objects.get(object_repr="MCP Asset").metadata["interface"],
            "mcp",
        )
        self.assertEqual(
            AuditLog.objects.get(object_repr="CLI Asset").metadata["interface"],
            "cli",
        )


class DocumentationSyncTests(TestCase):
    manifest = [
        {
            "doc_id": "rb-shared-service",
            "title": "Shared service",
            "doc_type": "runbook",
            "status": "active",
        }
    ]

    def test_sync_is_idempotent_and_prune_fails_closed(self):
        first = sync_documentation(
            self.manifest, interface="mcp", actor="test-agent"
        )
        second = sync_documentation(
            self.manifest, interface="mcp", actor="test-agent"
        )

        self.assertTrue(first["ok"])
        self.assertEqual(first["stats"]["created"], 1)
        self.assertEqual(second["stats"]["skipped"], 1)
        with self.assertRaisesRegex(ValueError, "confirm_prune"):
            sync_documentation(
                [],
                interface="mcp",
                actor="test-agent",
                prune_orphans=True,
            )

    def test_mcp_exposes_sync_through_the_same_service(self):
        async def call_sync():
            tool = mcp._tool_manager.get_tool("sync_documentation")
            return await tool.run({"manifest": self.manifest})

        result = async_to_sync(call_sync)()

        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["created"], 1)
