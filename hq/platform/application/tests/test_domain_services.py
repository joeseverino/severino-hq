"""Each domain's application service, reached the same way from every adapter."""

from datetime import UTC, date, datetime
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.projects.github import GitHubMetadataError, fetch_last_push
from hq.domains.projects.models import Project
from hq.platform.core.models import AuditLog
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.server import mcp

from ..assets import AssetCommand, save_asset
from ..capabilities import execute_capability
from ..content import ContentCommand, save_content
from ..documentation import sync_documentation
from ..expenses import ExpenseCommand, save_expense
from ..plugin_testing import form_keyed
from ..projects import ProjectCommand, refresh_project, save_project
from ..record_errors import RecordConflict, RecordNotFound
from ..security import OPERATOR_CAPABILITIES, Principal, cli_principal, mcp_principal


def as_mcp_caller(call):
    """Run an MCP tool call as an authenticated agent holding every grant.

    A tool reached with no caller bound is refused; these tests are about the
    shape the tool returns, so they bind the caller the door would have.
    """

    with set_principal(mcp_principal()):
        return async_to_sync(call)()


def mcp_write(name, payload):
    """Run a capability through the MCP tool as an agent would, with a retry key,
    which the tool requires of every change."""

    async def call():
        return await mcp.tools.get("execute_capability").call(
            {"name": name, "payload": payload | {"idempotency_key": f"example-{name}"}}
        )

    return as_mcp_caller(call)


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
        event = AuditLog.objects.get(object_type="Project", object_id=str(Project.objects.get().pk))
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

        with self.assertRaises(RecordConflict):
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
        pushed_at = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)

        result = refresh_project(
            project.slug,
            principal=Principal("operator", "web", OPERATOR_CAPABILITIES),
            github_fetcher=lambda repository_url, **kwargs: pushed_at,
        )

        self.assertTrue(result["github"]["ok"])
        project.refresh_from_db()
        self.assertEqual(project.last_push_at, pushed_at)
        event = AuditLog.objects.filter(object_type="Project", object_id=str(project.pk)).latest("created_at")
        self.assertEqual(event.metadata["operation"], "project.refresh")
        self.assertEqual(event.metadata["interface"], "web")

    def test_refresh_does_not_pass_itself_off_as_an_edit(self):
        """``updated_at`` answers when the operator last changed the project:
        the "Updated" column, the default sort and the model's ordering. It is
        also the ``expected_updated_at`` token. When the repository last moved
        is ``last_push_at``.
        """

        project = Project.objects.create(
            name="HQ",
            slug="hq",
            repository_url="https://github.com/joeseverino/severino-hq",
        )
        before = project.updated_at
        pushed_at = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)

        refresh_project(
            project.slug,
            principal=Principal("operator", "web", OPERATOR_CAPABILITIES),
            github_fetcher=lambda repository_url, **kwargs: pushed_at,
        )

        project.refresh_from_db()
        self.assertEqual(project.last_push_at, pushed_at)
        self.assertEqual(project.updated_at, before)


class AdapterParityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="joe", password="test-password")

    def test_web_and_mcp_create_projects_through_the_same_service(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("projects:create"),
            form_keyed(
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
                }
            ),
        )
        self.assertRedirects(response, reverse("projects:detail", args=["web-project"]))

        mcp_result = mcp_write(
            "project.create",
            {
                "name": "MCP Project",
                "slug": "mcp-project",
                "status": "active",
            },
        )

        self.assertEqual(set(mcp_result), {"ok", "created", "project"})
        self.assertEqual(
            AuditLog.objects.get(object_repr="Web Project").metadata["interface"],
            "web",
        )
        self.assertEqual(
            AuditLog.objects.get(object_repr="MCP Project").metadata["interface"],
            "mcp",
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
            principal=Principal("test-agent", "mcp", OPERATOR_CAPABILITIES),
        )

        self.assertEqual(result["asset"]["estimated_deductible_amount"], "799.99")
        self.assertEqual(result["asset"]["relationships"]["projects"], ["homelab"])
        event = AuditLog.objects.get(object_repr="Lab Server")
        self.assertEqual(event.metadata["operation"], "asset.create")
        self.assertEqual(event.metadata["interface"], "mcp")

    def test_missing_relationship_rolls_back_the_asset(self):
        with self.assertRaises(RecordNotFound):
            save_asset(
                AssetCommand(
                    item_name="Invalid",
                    slug="invalid",
                    related_projects=("missing-project",),
                ),
                principal=Principal("test-agent", "mcp", OPERATOR_CAPABILITIES),
            )

        self.assertFalse(Asset.objects.filter(slug="invalid").exists())

    def test_web_and_mcp_create_assets_through_the_same_service(self):
        user = get_user_model().objects.create_user(username="asset-operator", password="test-password")
        self.client.force_login(user)
        response = self.client.post(
            reverse("assets:create"),
            form_keyed(
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
                }
            ),
        )
        self.assertRedirects(response, reverse("assets:detail", args=["web-asset"]))

        mcp_result = mcp_write(
            "asset.create",
            {
                "item_name": "MCP Asset",
                "slug": "mcp-asset",
                "total_cost": "20.00",
                "related_projects": ["homelab"],
            },
        )

        self.assertEqual(set(mcp_result), {"ok", "created", "asset"})
        self.assertEqual(
            AuditLog.objects.get(object_repr="Web Asset").metadata["interface"],
            "web",
        )
        self.assertEqual(
            AuditLog.objects.get(object_repr="MCP Asset").metadata["interface"],
            "mcp",
        )


class DocumentationSyncTests(TestCase):
    manifest = [
        {
            "doc_id": "rb-shared-service",
            "title": "Shared service",
            "doc_type": "runbook",
            "status": "active",
            "system": "Example service",
            "environment": "other",
            "sensitivity": "internal",
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
        result = mcp_write("documentation.sync", {"manifest": self.manifest})

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
            form_keyed(
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
                }
            ),
        )
        self.assertRedirects(response, reverse("docs_index:detail", args=["rb-web-doc"]))
        self.assertEqual(
            AuditLog.objects.get(object_repr="rb-web-doc\xa0· Web Doc").metadata["operation"],
            "documentation.create",
        )


class ContentApplicationServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Site", slug="site")

    def test_relationship_failure_rolls_back(self):
        with self.assertRaises(RecordNotFound):
            save_content(
                ContentCommand(
                    title="Invalid",
                    slug="invalid",
                    related_assets=("missing",),
                ),
                principal=Principal("agent", "mcp", OPERATOR_CAPABILITIES),
            )
        self.assertFalse(ContentItem.objects.filter(slug="invalid").exists())

    def test_web_and_mcp_share_the_content_service_and_audit(self):
        user = get_user_model().objects.create_user(username="content-operator")
        self.client.force_login(user)
        response = self.client.post(
            reverse("content:create"),
            form_keyed(
                {
                    "title": "Web Content",
                    "slug": "web-content",
                    "content_type": "article",
                    "status": "draft",
                    "topic": "",
                    "tags": "django, mcp",
                    "published_url": "",
                    "published_at": "",
                    "notes": "",
                    "related_projects": [self.project.pk],
                    "related_assets": [],
                    "related_expenses": [],
                    "related_documentation": [],
                }
            ),
        )
        self.assertRedirects(response, reverse("content:detail", args=["web-content"]))

        mcp_result = mcp_write(
            "content.create",
            {
                "title": "MCP Content",
                "slug": "mcp-content",
                "related_projects": ["site"],
            },
        )

        self.assertEqual(mcp_result["content"]["relationships"]["projects"], ["site"])
        for title, interface in (
            ("Web Content", "web"),
            ("MCP Content", "mcp"),
        ):
            self.assertEqual(
                AuditLog.objects.get(object_repr=title).metadata["interface"],
                interface,
            )


class ExpenseApplicationServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Ops", slug="ops")

    def test_relationship_failure_rolls_back(self):
        with self.assertRaises(RecordNotFound):
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

    def test_web_and_mcp_share_the_expense_service_and_audit(self):
        user = get_user_model().objects.create_user(username="expense-operator")
        self.client.force_login(user)
        response = self.client.post(
            reverse("expenses:create"),
            form_keyed(
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
                }
            ),
        )
        self.assertEqual(response.status_code, 302)

        mcp_result = mcp_write(
            "expense.create",
            {
                "date": "2026-07-25",
                "vendor": "MCP Vendor",
                "item": "MCP Expense",
                "total_cost": "20.00",
                "related_project": "ops",
            },
        )

        self.assertEqual(mcp_result["expense"]["related_project"], "ops")
        self.assertEqual(
            {event.metadata["interface"] for event in AuditLog.objects.filter(metadata__operation="expense.create")},
            {"web", "mcp"},
        )
