from __future__ import annotations

import json
from io import StringIO
from decimal import Decimal
from datetime import date, datetime, timezone
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from assets.models import Asset
from content.models import ContentItem
from expenses.models import Expense
from receipts.models import Receipt
from core.models import AuditLog
from hq_mcp.server import mcp
from projects.models import Project
from projects.github import GitHubMetadataError, fetch_last_push
from docs_index.models import DocumentationRecord

from .documentation import sync_documentation
from .assets import AssetCommand, NotFoundError as AssetNotFoundError, save_asset
from .content import ContentCommand, NotFoundError as ContentNotFoundError, save_content
from .capabilities import (
    CapabilitySpec,
    capability_specs,
    describe_capabilities,
    execute_capability,
)
from .expenses import ExpenseCommand, NotFoundError as ExpenseNotFoundError, save_expense
from .projects import ConflictError, ProjectCommand, refresh_project, save_project
from .security import (
    OPERATOR_CAPABILITIES,
    AuthorizationError,
    Principal,
    cli_principal,
    mcp_principal,
)


class CapabilityTests(TestCase):
    def test_registry_emits_stable_json_schemas_and_effects(self):
        first = describe_capabilities()
        second = describe_capabilities()

        self.assertEqual(first, second)
        project = next(
            item for item in first["capabilities"] if item["name"] == "project.create"
        )
        self.assertEqual(project["effect"], "remote_write")
        self.assertIn("name", project["input_schema"]["properties"])

    def test_plugin_capabilities_fail_fast_on_an_unknown_effect(self):
        malformed = CapabilitySpec(
            "example.bad",
            "Bad effect",
            "maybe_writes",
            "example.write",
            ProjectCommand,
            save_project,
        )
        with (
            mock.patch(
                "application.capabilities.plugin_capability_specs",
                return_value=(malformed,),
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "invalid effect"),
        ):
            capability_specs()

    def test_plugin_capabilities_fail_fast_on_a_wrong_handler_contract(self):
        def wrong_handler(command):
            return command

        malformed = CapabilitySpec(
            "example.bad",
            "Bad handler",
            "remote_write",
            "example.write",
            ProjectCommand,
            wrong_handler,
        )
        with (
            mock.patch(
                "application.capabilities.plugin_capability_specs",
                return_value=(malformed,),
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "host call contract"),
        ):
            capability_specs()

    def test_mcp_writes_fail_closed_by_default(self):
        with self.assertRaisesRegex(AuthorizationError, "write_projects"):
            save_project(
                ProjectCommand(name="Denied", slug="denied"),
                principal=mcp_principal(),
            )
        self.assertFalse(Project.objects.filter(slug="denied").exists())

    def test_json_executor_returns_canonical_error_envelope(self):
        result = execute_capability(
            "project.create",
            {"slug": "missing-name"},
            principal=cli_principal(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_a_target_of_the_wrong_kind_is_refused_by_name(self):
        """The message names the kind expected, so a client can correct itself.

        Reached only after authorization and payload validation have passed:
        a caller who may not run the capability is told that instead, and
        learns nothing about the shape of target it would have taken.
        """

        result = execute_capability(
            "expense.update",
            {
                "date": "2026-01-01",
                "vendor": "V",
                "item": "I",
                "total_cost": "1.00",
                "business_use_percentage": 100,
            },
            principal=cli_principal(),
            target="not-an-integer",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertEqual(
            result["error"]["message"], "expense.update requires a integer target."
        )

    def test_an_unauthorized_caller_is_refused_before_its_target_is_read(self):
        result = execute_capability(
            "expense.update",
            {},
            principal=Principal("nobody", "test", frozenset()),
            target="not-an-integer",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")

    def test_json_executor_creates_through_same_service(self):
        result = execute_capability(
            "project.create",
            {"name": "JSON Project", "slug": "json-project"},
            principal=cli_principal(),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(Project.objects.filter(slug="json-project").exists())

    def test_upsert_capability_is_idempotent_and_server_owned(self):
        created = execute_capability(
            "project.upsert",
            {"name": "First", "slug": "same-project"},
            principal=cli_principal(),
        )
        updated = execute_capability(
            "project.upsert",
            {"name": "Second", "slug": "same-project"},
            principal=cli_principal(),
        )

        self.assertTrue(created["created"])
        self.assertFalse(updated["created"])
        self.assertEqual(Project.objects.get().name, "Second")

    def test_hq_sync_refuses_a_payload_it_no_longer_understands(self):
        """The topology it used to carry is HQ's own now, so nothing sends one.

        Refused rather than ignored: a caller still sending one is describing a
        world HQ derives, and accepting it quietly would leave them believing
        the document still governed something.
        """

        result = execute_capability(
            "hq.sync",
            {
                "manifest": [
                    {
                        "doc_id": "rb-atomic-sync",
                        "title": "Atomic sync",
                        "doc_type": "runbook",
                        "status": "active",
                    }
                ],
                "topology": {"version": 3},
            },
            principal=cli_principal(),
        )

        self.assertFalse(result["ok"])
        self.assertFalse(
            DocumentationRecord.objects.filter(doc_id="rb-atomic-sync").exists()
        )

    def test_delete_requires_exact_confirmation(self):
        project = Project.objects.create(name="Keep Me", slug="keep-me")

        result = execute_capability(
            "project.delete",
            {"confirm": "wrong-target"},
            principal=cli_principal(),
            target=project.slug,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "operation_failed")
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

    def test_delete_honors_optimistic_concurrency_and_audits_success(self):
        project = Project.objects.create(name="Delete Me", slug="delete-me")
        stale_timestamp = project.updated_at.isoformat()
        project.name = "Changed elsewhere"
        project.save()

        stale = execute_capability(
            "project.delete",
            {"confirm": project.slug},
            principal=cli_principal(),
            target=project.slug,
            expected_updated_at=stale_timestamp,
        )
        self.assertFalse(stale["ok"])
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

        deleted = execute_capability(
            "project.delete",
            {"confirm": project.slug},
            principal=cli_principal(),
            target=project.slug,
            expected_updated_at=project.updated_at.isoformat(),
        )
        self.assertTrue(deleted["ok"])
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())
        event = AuditLog.objects.get(
            action=AuditLog.Action.DELETED,
            object_type="Project",
            object_id=str(project.pk),
        )
        self.assertEqual(event.metadata["operation"], "project.delete")
        self.assertEqual(event.metadata["interface"], "cli")

    @override_settings(
        SEVERINO_MCP_ENABLE_WRITES=True,
        SEVERINO_MCP_ENABLE_DELETES=False,
    )
    def test_mcp_deletes_are_gated_separately_from_writes(self):
        project = Project.objects.create(name="Protected", slug="protected")

        result = execute_capability(
            "project.delete",
            {"confirm": project.slug},
            principal=mcp_principal(),
            target=project.slug,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

    def test_cli_describe_and_run_use_the_same_registry(self):
        described = StringIO()
        call_command("hq_capability", "describe", stdout=described)
        self.assertEqual(json.loads(described.getvalue()), describe_capabilities())

        executed = StringIO()
        call_command(
            "hq_capability",
            "run",
            "project.create",
            payload='{"name":"CLI JSON","slug":"cli-json"}',
            stdout=executed,
        )
        self.assertTrue(json.loads(executed.getvalue())["ok"])

    def test_receipt_json_capability_updates_metadata_without_file_access(self):
        receipt = Receipt.objects.create(
            file="receipts/fixture.pdf",
            original_filename="fixture.pdf",
        )
        result = execute_capability(
            "receipt.update",
            {"vendor": "Updated Vendor", "amount": "42.50"},
            principal=cli_principal(),
            target=receipt.id,
        )

        self.assertTrue(result["ok"])
        receipt.refresh_from_db()
        self.assertEqual(receipt.vendor, "Updated Vendor")
        self.assertEqual(receipt.original_filename, "fixture.pdf")

    @override_settings(
        SEVERINO_MCP_ENABLE_WRITES=True,
        SEVERINO_MCP_ENABLE_PRUNE=False,
    )
    def test_mcp_prune_is_a_separate_capability(self):
        with self.assertRaisesRegex(AuthorizationError, "prune_documentation"):
            sync_documentation(
                [],
                principal=mcp_principal(),
                prune_orphans=True,
                confirm_prune=True,
            )


class ProjectApplicationServiceTests(TestCase):
    def test_github_gateway_rejects_non_repository_urls_before_network_access(self):
        with self.assertRaises(GitHubMetadataError):
            fetch_last_push("https://example.com/joeseverino/severino-hq")

    def test_service_owns_validation_transaction_and_audit_context(self):
        result = save_project(
            ProjectCommand(name="Shared HQ", slug="shared-hq", status="active"),
            principal=Principal("test-agent", "mcp", OPERATOR_CAPABILITIES),
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
                principal=Principal("test-agent", "mcp", OPERATOR_CAPABILITIES),
            )
        self.assertFalse(Project.objects.filter(slug="invalid").exists())

    def test_update_supports_optimistic_concurrency(self):
        created = save_project(
            ProjectCommand(name="HQ", slug="hq"),
            principal=Principal("operator", "cli", OPERATOR_CAPABILITIES),
        )
        project = Project.objects.get(slug="hq")
        project.name = "Changed elsewhere"
        project.save()

        with self.assertRaises(ConflictError):
            save_project(
                ProjectCommand(name="Stale write", slug="hq"),
                principal=Principal("agent", "mcp", OPERATOR_CAPABILITIES),
                current_slug="hq",
                expected_updated_at=created["project"]["updated_at"],
            )

    def test_refresh_owns_external_metadata_persistence_and_audit(self):
        project = Project.objects.create(
            name="HQ",
            slug="hq",
            repository_url="https://github.com/joeseverino/severino-hq",
        )
        pushed_at = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)

        result = refresh_project(
            project.slug,
            principal=Principal("operator", "web", OPERATOR_CAPABILITIES),
            github_fetcher=lambda repository_url, **kwargs: pushed_at,
        )

        self.assertTrue(result["github"]["ok"])
        project.refresh_from_db()
        self.assertEqual(project.last_push_at, pushed_at)
        event = AuditLog.objects.filter(
            object_type="Project", object_id=str(project.pk)
        ).latest("created_at")
        self.assertEqual(event.metadata["operation"], "project.refresh")
        self.assertEqual(event.metadata["interface"], "web")


@override_settings(SEVERINO_MCP_ENABLE_WRITES=True)
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
            tool = mcp._tool_manager.get_tool("execute_capability")
            return await tool.run(
                {
                    "name": "project.create",
                    "payload": {
                        "name": "MCP Project",
                        "slug": "mcp-project",
                        "status": "active",
                    },
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


@override_settings(SEVERINO_MCP_ENABLE_WRITES=True)
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
            principal=Principal("test-agent", "mcp", OPERATOR_CAPABILITIES),
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
                principal=Principal("test-agent", "mcp", OPERATOR_CAPABILITIES),
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
            tool = mcp._tool_manager.get_tool("execute_capability")
            return await tool.run(
                {
                    "name": "asset.create",
                    "payload": {
                        "item_name": "MCP Asset",
                        "slug": "mcp-asset",
                        "total_cost": "20.00",
                        "related_projects": ["homelab"],
                    },
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


@override_settings(
    SEVERINO_MCP_ENABLE_WRITES=True,
    SEVERINO_MCP_ENABLE_PRUNE=True,
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
            self.manifest,
            principal=Principal("test-agent", "mcp", OPERATOR_CAPABILITIES),
        )
        second = sync_documentation(
            self.manifest,
            principal=Principal("test-agent", "mcp", OPERATOR_CAPABILITIES),
        )

        self.assertTrue(first["ok"])
        self.assertEqual(first["stats"]["created"], 1)
        self.assertEqual(second["stats"]["skipped"], 1)
        with self.assertRaisesRegex(ValueError, "confirm_prune"):
            sync_documentation(
                [],
                principal=Principal("test-agent", "mcp", OPERATOR_CAPABILITIES),
                prune_orphans=True,
            )

    def test_mcp_exposes_sync_through_the_same_service(self):
        async def call_sync():
            tool = mcp._tool_manager.get_tool("execute_capability")
            return await tool.run(
                {
                    "name": "documentation.sync",
                    "payload": {"manifest": self.manifest},
                }
            )

        result = async_to_sync(call_sync)()

        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["created"], 1)

    def test_json_documentation_capability_redacts_restricted_fields(self):
        result = execute_capability(
            "documentation.create",
            {
                "doc_id": "rb-private",
                "title": "Private",
                "sensitivity": "restricted",
                "obsidian_path": "Secret/Private.md",
                "notes": "never return this",
            },
            principal=cli_principal(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["documentation"]["obsidian_path"], "")
        self.assertEqual(result["documentation"]["notes"], "")

    def test_web_documentation_create_uses_application_service(self):
        user = get_user_model().objects.create_user(username="docs-operator")
        self.client.force_login(user)
        response = self.client.post(
            reverse("docs_index:create"),
            {
                "doc_id": "rb-web-doc",
                "title": "Web Doc",
                "doc_type": "runbook",
                "system_service": "",
                "environment": "other",
                "status": "active",
                "sensitivity": "internal",
                "obsidian_path": "",
                "github_path": "",
                "external_url": "",
                "last_reviewed": "",
                "notes": "",
                "related_projects": [],
                "related_assets": [],
                "related_expenses": [],
            },
        )
        self.assertRedirects(
            response, reverse("docs_index:detail", args=["rb-web-doc"])
        )
        self.assertEqual(
            AuditLog.objects.get(object_repr="rb-web-doc — Web Doc").metadata[
                "operation"
            ],
            "documentation.create",
        )


@override_settings(SEVERINO_MCP_ENABLE_WRITES=True)
class ContentApplicationServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Site", slug="site")

    def test_relationship_failure_rolls_back(self):
        with self.assertRaisesRegex(ContentNotFoundError, "missing"):
            save_content(
                ContentCommand(
                    title="Invalid",
                    slug="invalid",
                    related_assets=("missing",),
                ),
                principal=Principal("agent", "mcp", OPERATOR_CAPABILITIES),
            )
        self.assertFalse(ContentItem.objects.filter(slug="invalid").exists())

    def test_web_mcp_and_cli_share_content_shape_and_audit(self):
        user = get_user_model().objects.create_user(username="content-operator")
        self.client.force_login(user)
        response = self.client.post(
            reverse("content:create"),
            {
                "title": "Web Content",
                "slug": "web-content",
                "content_type": "article",
                "status": "draft",
                "topic": "",
                "tags": "django, mcp",
                "published_url": "",
                "wordpress_post_id": "",
                "wordpress_slug": "",
                "published_at": "",
                "notes": "",
                "related_projects": [self.project.pk],
                "related_assets": [],
                "related_expenses": [],
                "related_documentation": [],
            },
        )
        self.assertRedirects(
            response, reverse("content:detail", args=["web-content"])
        )

        async def call_mcp():
            tool = mcp._tool_manager.get_tool("execute_capability")
            return await tool.run(
                {
                    "name": "content.create",
                    "payload": {
                        "title": "MCP Content",
                        "slug": "mcp-content",
                        "related_projects": ["site"],
                    },
                }
            )

        mcp_result = async_to_sync(call_mcp)()
        output = StringIO()
        call_command(
            "create_content",
            "cli-content",
            title="CLI Content",
            project=["site"],
            json=True,
            stdout=output,
        )
        cli_result = json.loads(output.getvalue())

        self.assertEqual(set(mcp_result["content"]), set(cli_result["content"]))
        for title, interface in (
            ("Web Content", "web"),
            ("MCP Content", "mcp"),
            ("CLI Content", "cli"),
        ):
            self.assertEqual(
                AuditLog.objects.get(object_repr=title).metadata["interface"],
                interface,
            )


@override_settings(SEVERINO_MCP_ENABLE_WRITES=True)
class ExpenseApplicationServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Ops", slug="ops")

    def test_relationship_failure_rolls_back(self):
        with self.assertRaisesRegex(ExpenseNotFoundError, "missing"):
            save_expense(
                ExpenseCommand(
                    date=date(2026, 7, 25),
                    vendor="Vendor",
                    item="Invalid",
                    related_project="missing",
                ),
                principal=cli_principal(),
            )
        self.assertEqual(Expense.objects.count(), 0)

    def test_web_mcp_and_cli_share_expense_shape(self):
        user = get_user_model().objects.create_user(username="expense-operator")
        self.client.force_login(user)
        response = self.client.post(
            reverse("expenses:create"),
            {
                "date": "2026-07-25",
                "vendor": "Web Vendor",
                "item": "Web Expense",
                "category": "software",
                "total_cost": "100.00",
                "business_use_percentage": "75",
                "payment_method": "credit",
                "business_purpose": "",
                "notes": "",
                "related_project": self.project.pk,
                "related_asset": "",
                "related_content": "",
                "related_documentation": "",
            },
        )
        self.assertEqual(response.status_code, 302)

        async def call_mcp():
            tool = mcp._tool_manager.get_tool("execute_capability")
            return await tool.run(
                {
                    "name": "expense.create",
                    "payload": {
                        "date": "2026-07-25",
                        "vendor": "MCP Vendor",
                        "item": "MCP Expense",
                        "total_cost": "20.00",
                        "related_project": "ops",
                    },
                }
            )

        mcp_result = async_to_sync(call_mcp)()
        output = StringIO()
        call_command(
            "create_expense",
            date="2026-07-25",
            vendor="CLI Vendor",
            item="CLI Expense",
            cost="30.00",
            project="ops",
            json=True,
            stdout=output,
        )
        cli_result = json.loads(output.getvalue())
        self.assertEqual(set(mcp_result["expense"]), set(cli_result["expense"]))
        self.assertEqual(mcp_result["expense"]["related_project"], "ops")


class DocumentationSyncCapabilityTests(SimpleTestCase):
    """`hq sync` must be grantable without handing over every other write."""

    def test_doc_sync_flag_grants_only_documentation_sync(self):
        from application.security import AuthorizationError, Capability, mcp_principal

        with self.settings(
            SEVERINO_MCP_ENABLE_DOC_SYNC=True, SEVERINO_MCP_ENABLE_WRITES=False
        ):
            principal = mcp_principal()
        principal.require(Capability.SYNC_DOCUMENTATION)
        for withheld in (
            Capability.WRITE_EXPENSES,
            Capability.WRITE_RECEIPTS,
            Capability.WRITE_PROJECTS,
            Capability.WRITE_ASSETS,
            Capability.WRITE_CONTENT,
            Capability.WRITE_DOCUMENTATION,
        ):
            with self.assertRaises(AuthorizationError):
                principal.require(withheld)

    def test_broad_writes_still_imply_doc_sync(self):
        from application.security import Capability, mcp_principal

        with self.settings(
            SEVERINO_MCP_ENABLE_WRITES=True, SEVERINO_MCP_ENABLE_DOC_SYNC=False
        ):
            mcp_principal().require(Capability.SYNC_DOCUMENTATION)

    def test_both_flags_off_withholds_doc_sync(self):
        from application.security import AuthorizationError, Capability, mcp_principal

        with self.settings(
            SEVERINO_MCP_ENABLE_WRITES=False, SEVERINO_MCP_ENABLE_DOC_SYNC=False
        ):
            with self.assertRaises(AuthorizationError):
                mcp_principal().require(Capability.SYNC_DOCUMENTATION)


class InfrastructureCapabilityTests(SimpleTestCase):
    """Declaring topology and requesting certificates are different powers."""

    def test_infrastructure_flag_does_not_grant_certificate_renewal(self):
        from application.security import AuthorizationError, Capability, mcp_principal

        with self.settings(
            SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True,
            SEVERINO_MCP_ENABLE_CERT_RENEWAL=False,
        ):
            principal = mcp_principal()
        principal.require(Capability.MANAGE_INFRASTRUCTURE)
        with self.assertRaises(AuthorizationError):
            principal.require(Capability.REQUEST_CERTIFICATE_RENEWAL)

    def test_certificate_renewal_is_grantable_on_its_own(self):
        from application.security import AuthorizationError, Capability, mcp_principal

        with self.settings(
            SEVERINO_MCP_ENABLE_INFRASTRUCTURE=False,
            SEVERINO_MCP_ENABLE_CERT_RENEWAL=True,
        ):
            principal = mcp_principal()
        principal.require(Capability.REQUEST_CERTIFICATE_RENEWAL)
        with self.assertRaises(AuthorizationError):
            principal.require(Capability.MANAGE_INFRASTRUCTURE)


class MultipleFileFieldTests(SimpleTestCase):
    """Several files selected must mean several files imported.

    Django's FileField binds one upload; pointed at a multiple input it keeps
    whichever the widget returned and drops the rest silently -- an operator
    would see one file land and no explanation for the others.
    """

    @staticmethod
    def _upload(name, body=b"x"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, body)

    def test_every_selected_file_is_returned(self):
        from django.http import QueryDict
        from django.utils.datastructures import MultiValueDict

        from .forms import MultipleFileField

        field = MultipleFileField()
        files = MultiValueDict({"f": [self._upload("a.csv"), self._upload("b.csv")]})
        data = field.widget.value_from_datadict(QueryDict(), files, "f")
        self.assertEqual([item.name for item in field.clean(data)], ["a.csv", "b.csv"])

    def test_widget_reads_the_full_list_from_the_request(self):
        from .forms import MultipleFileField

        # The multiple attribute has to come from the widget: rendering it while
        # reading a single file is how uploads get dropped without an error.
        self.assertTrue(MultipleFileField().widget.attrs.get("multiple"))

    def test_each_file_is_validated_not_just_the_first(self):
        from django.core.exceptions import ValidationError as FormValidationError

        from .forms import MultipleFileField

        field = MultipleFileField(max_length=5)
        with self.assertRaises(FormValidationError):
            field.clean([self._upload("ok.csv"), self._upload("far-too-long.csv")])

    def test_no_selection_raises_the_ordinary_required_error(self):
        from django.core.exceptions import ValidationError as FormValidationError

        from .forms import MultipleFileField

        with self.assertRaises(FormValidationError):
            MultipleFileField().clean([])
        self.assertEqual(MultipleFileField(required=False).clean([]), [])


class ListRowTests(SimpleTestCase):
    """State on a shared row is never carried by colour alone."""

    def test_status_requires_its_own_text(self):
        from .ui import ListRow

        ListRow(title="Import", status="attention", badge="Needs review")
        with self.assertRaises(ValueError):
            ListRow(title="Import", status="attention")

    def test_status_must_come_from_the_one_vocabulary(self):
        from .ui import ListRow

        with self.assertRaises(ValueError):
            ListRow(title="Import", status="warning", badge="Warning")


class TableTotalsTests(TestCase):
    """Totals describe the filtered set, not the page you happen to be on."""

    def setUp(self):
        from decimal import Decimal

        from assets.models import Asset

        for index in range(5):
            Asset.objects.create(
                item_name=f"Item {index}",
                slug=f"item-{index}",
                total_cost=Decimal("100.00"),
                category="tools" if index < 2 else "office",
            )

    def _view(self, **attrs):
        from application.tables import TableListMixin, TableTotal
        from assets.models import Asset

        class View(TableListMixin):
            table_totals = (TableTotal("total_cost", "Total cost"),)

        view = View()
        for key, value in attrs.items():
            setattr(view, key, value)
        return view, Asset.objects.all()

    def test_it_sums_the_whole_queryset(self):
        from decimal import Decimal

        view, queryset = self._view()
        totals = view.table_totals_for(queryset)
        self.assertEqual(totals["total_cost"]["value"], Decimal("500.00"))
        self.assertEqual(totals["total_cost"]["label"], "Total cost")

    def test_it_follows_the_filter(self):
        from decimal import Decimal

        view, queryset = self._view()
        totals = view.table_totals_for(queryset.filter(category="tools"))
        self.assertEqual(totals["total_cost"]["value"], Decimal("200.00"))

    def test_a_view_declaring_no_totals_gets_none(self):
        from application.tables import TableListMixin

        class Bare(TableListMixin):
            pass

        from assets.models import Asset

        self.assertEqual(Bare().table_totals_for(Asset.objects.all()), {})

    def test_a_missing_queryset_is_not_an_error(self):
        view, _ = self._view()
        self.assertEqual(view.table_totals_for(None), {})


class WriteSpineContractTests(TestCase):
    """What a write view must declare, enforced where the fix is obvious.

    Both guards exist because the failure they replace is quiet. A service left
    as a plain function binds as a method and passes the view where the command
    belongs; a missing noun renders a success message with a hole in it.
    """

    def _view(self, **attributes):
        from .writes import ServiceCreateMixin

        return type("AView", (ServiceCreateMixin,), attributes)

    def test_a_service_that_is_not_a_staticmethod_is_refused(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            self._view(service=lambda command, **kwargs: None)

        self.assertIn("staticmethod", str(caught.exception))

    def test_a_view_that_writes_must_say_what_it_writes(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            self._view(service=staticmethod(lambda command, **kwargs: None))

        self.assertIn("noun", str(caught.exception))
        self.assertIn("result_key", str(caught.exception))

    def test_a_complete_view_is_accepted(self):
        view = self._view(
            service=staticmethod(lambda command, **kwargs: None),
            noun="Thing",
            result_key="thing",
            identity_attr="slug",
        )

        self.assertEqual(view.noun, "Thing")

    def test_a_mixin_that_supplies_no_service_is_not_a_view(self):
        """Intermediate layers declare part of the contract and finish none of
        it, so completeness is asked of the class that supplies the service."""

        from .writes import ServiceCreateMixin

        layer = type("PartialLayer", (ServiceCreateMixin,), {"noun": "Thing"})

        self.assertEqual(layer.noun, "Thing")

    def test_the_base_declares_no_noun_to_collide_with(self):
        """A placeholder value that is never correct still wins or loses by base
        order, so which one applies depends on how the bases were written."""

        from .writes import ServiceWriteMixin

        for name in ("noun", "result_key", "identity_attr"):
            self.assertNotIn(name, vars(ServiceWriteMixin))
