"""Smoke tests for Severino HQ.

These don't aim for exhaustive coverage — they verify that the auth gate works
and every page in the main nav (including reports + exports) renders without a
500. Add module-specific tests inside each app as it grows.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from assets.models import Asset
from core.oidc import HQOIDCAuthenticationBackend
from content.models import ContentItem
from core.models import AuditLog
from docs_index.models import DocumentationRecord
from docs_index.importer import (
    ManifestImportError,
    import_manifest_data,
    validate_manifest_data,
)
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt


User = get_user_model()


class AuthGateTests(TestCase):
    def test_anonymous_dashboard_redirects_to_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_login_page_is_accessible(self):
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)

    @override_settings(SEVERINO_OIDC_ENABLED=True)
    def test_login_page_shows_sso_button_when_enabled(self):
        response = self.client.get("/accounts/login/")
        self.assertContains(response, "Sign in with SSO")


class OIDCBackendTests(TestCase):
    def test_allows_user_in_allowed_group(self):
        backend = HQOIDCAuthenticationBackend()

        with override_settings(
            SEVERINO_OIDC_ALLOWED_EMAILS=set(),
            SEVERINO_OIDC_ALLOWED_GROUPS={"admins"},
        ):
            self.assertTrue(
                backend.verify_claims(
                    {
                        "preferred_username": "joe",
                        "groups": ["admins"],
                    }
                )
            )

    def test_matches_existing_user_by_preferred_username_without_email(self):
        user = User.objects.create_user(username="joe")
        backend = HQOIDCAuthenticationBackend()

        users = backend.filter_users_by_claims(
            {"preferred_username": "joe", "groups": ["admins"]}
        )

        self.assertEqual(list(users), [user])

    def test_allows_user_by_allowed_email(self):
        backend = HQOIDCAuthenticationBackend()

        with override_settings(
            SEVERINO_OIDC_ALLOWED_EMAILS={"joe@example.com"},
            SEVERINO_OIDC_ALLOWED_GROUPS=set(),
        ):
            self.assertTrue(
                backend.verify_claims(
                    {
                        "email": "joe@example.com",
                        "groups": [],
                    }
                )
            )

    def test_rejects_when_no_allow_rule_is_configured(self):
        backend = HQOIDCAuthenticationBackend()

        with override_settings(
            SEVERINO_OIDC_ALLOWED_EMAILS=set(),
            SEVERINO_OIDC_ALLOWED_GROUPS=set(),
        ):
            with self.assertRaises(PermissionDenied):
                backend.verify_claims(
                    {
                        "preferred_username": "joe",
                        "groups": ["admins"],
                    }
                )


class _AuthedTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="tester",
            email="t@example.com",
            password="strongtestpass-1234",
        )

    def setUp(self):
        self.client = Client()
        assert self.client.login(
            username="tester", password="strongtestpass-1234"
        )


class NavigationSmokeTests(_AuthedTestCase):
    URLS = [
        "/",
        "/search/",
        "/projects/",
        "/projects/new/",
        "/content/",
        "/content/new/",
        "/docs/",
        "/docs/new/",
        "/docs/import/",
        "/assets/",
        "/assets/new/",
        "/expenses/",
        "/expenses/new/",
        "/receipts/",
        "/receipts/new/",
        "/reports/",
        "/audit/",
    ]

    def test_all_main_pages_render(self):
        for url in self.URLS:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 200,
                    f"{url} returned {response.status_code}",
                )


class DashboardWorkflowTests(_AuthedTestCase):
    def test_dashboard_surfaces_missing_project_output_once_in_queue(self):
        Project.objects.create(name="Documentable lab", status=Project.Status.ACTIVE)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open items")
        self.assertContains(response, "Priority queue")
        self.assertContains(response, "Active projects need output")
        self.assertContains(response, "Documentable lab")
        self.assertContains(response, "/projects/?needs_output=1")
        self.assertNotContains(response, "Needs attention")
        self.assertNotContains(response, "Project opportunities")
        self.assertNotContains(response, "Relationship health")
        self.assertNotContains(response, "Docs by system")

    def test_dashboard_excludes_site_pages_from_docs_review(self):
        DocumentationRecord.objects.create(
            doc_id="page-about",
            title="About page",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            status=DocumentationRecord.Status.ACTIVE,
        )

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "All active docs reviewed recently.")
        self.assertNotContains(response, "Review queue")

    def test_projects_can_filter_for_missing_output(self):
        project = Project.objects.create(
            name="No output yet", status=Project.Status.ACTIVE
        )
        documented = Project.objects.create(
            name="Documented", status=Project.Status.ACTIVE
        )
        doc = DocumentationRecord.objects.create(
            doc_id="rb-documented-001",
            title="Documented runbook",
            status=DocumentationRecord.Status.ACTIVE,
        )
        doc.related_projects.add(documented)
        content = ContentItem.objects.create(title="Documented post")
        content.related_projects.add(documented)

        response = self.client.get("/projects/?needs_output=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, project.name)
        self.assertNotContains(response, documented.name)

    def test_archived_projects_sort_last_by_default(self):
        active = Project.objects.create(
            name="ZZZ active project", status=Project.Status.ACTIVE
        )
        archived = Project.objects.create(
            name="AAA archived project", status=Project.Status.ARCHIVED
        )

        response = self.client.get("/projects/")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertLess(body.index(active.name), body.index(archived.name))

    @override_settings(SEVERINO_DOC_REVIEW_INTERVAL_DAYS=30)
    def test_docs_review_filter_uses_configured_interval(self):
        current = DocumentationRecord.objects.create(
            doc_id="rb-current-001",
            title="Current runbook",
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=20),
        )
        stale = DocumentationRecord.objects.create(
            doc_id="rb-stale-001",
            title="Stale runbook",
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )
        site_page = DocumentationRecord.objects.create(
            doc_id="page-stale-001",
            title="Stale site page",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )

        response = self.client.get("/docs/?needs_review=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, stale.title)
        self.assertNotContains(response, current.title)
        self.assertNotContains(response, site_page.title)


class ExportSmokeTests(_AuthedTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name="Lab — homelab DNS", status=Project.Status.ACTIVE
        )
        self.asset = Asset.objects.create(
            item_name="Test switch",
            total_cost=Decimal("29.00"),
            business_use_percentage=100,
            purchase_date=date.today(),
            status=Asset.Status.ACTIVE,
        )
        self.expense = Expense.objects.create(
            date=date.today(),
            vendor="Cloudflare",
            item="Cloudflare Pro",
            category="hosting",
            total_cost=Decimal("240.00"),
            business_use_percentage=100,
            related_project=self.project,
        )
        self.doc = DocumentationRecord.objects.create(
            doc_id="rb-test-001",
            title="Test runbook",
            obsidian_path="Infra/Test.md",
        )
        self.content = ContentItem.objects.create(
            title="Test article", status=ContentItem.Status.DRAFT
        )

    def test_csv_exports(self):
        for url in [
            "/reports/export/expenses.csv",
            "/reports/export/assets.csv",
            "/reports/export/content.csv",
            "/reports/export/projects.csv",
            "/reports/export/documentation.csv",
        ]:
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200, url)
                self.assertEqual(r["Content-Type"].split(";")[0], "text/csv")

    def test_year_summary_json_has_relationships(self):
        r = self.client.get("/reports/export/year-summary.json")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.content.decode("utf-8"))
        # Stable slugs / doc_ids exposed for the future MCP.
        slugs = {p["slug"] for p in data["projects"]}
        self.assertIn(self.project.slug, slugs)
        doc_ids = {d["doc_id"] for d in data["documentation"]}
        self.assertIn(self.doc.doc_id, doc_ids)
        # Disclaimer present and deductible math reflected.
        self.assertIn("not tax advice", data["disclaimer"].lower())

    def test_year_summary_markdown_is_ai_readable(self):
        r = self.client.get("/reports/export/year-summary.md")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode("utf-8")
        self.assertIn("# Severino HQ year summary", body)
        self.assertIn("Cloudflare", body)
        self.assertIn("Test runbook", body)


class ManifestImportTests(TestCase):
    def test_manifest_backfills_blank_project_technologies_from_tags(self):
        project = Project.objects.create(
            name="Techless project",
            slug="techless-project",
            status=Project.Status.ACTIVE,
        )

        stats = import_manifest_data(
            [
                {
                    "doc_id": "project-techless-project",
                    "title": "Techless project",
                    "doc_type": DocumentationRecord.DocType.ARCHITECTURE_NOTE,
                    "status": DocumentationRecord.Status.ACTIVE,
                    "related_projects": [project.slug],
                    "tags": ["django", "sqlite", "tailscale"],
                }
            ]
        )

        project.refresh_from_db()
        self.assertEqual(project.technologies_used, "django, sqlite, tailscale")
        self.assertEqual(stats["projects_tech_backfilled"], 1)

    def test_manifest_does_not_overwrite_curated_project_technologies(self):
        project = Project.objects.create(
            name="Curated project",
            slug="curated-project",
            status=Project.Status.ACTIVE,
            technologies_used="Django, PostgreSQL",
        )

        stats = import_manifest_data(
            [
                {
                    "doc_id": "project-curated-project",
                    "title": "Curated project",
                    "doc_type": DocumentationRecord.DocType.ARCHITECTURE_NOTE,
                    "status": DocumentationRecord.Status.ACTIVE,
                    "related_projects": [project.slug],
                    "tags": ["sqlite", "tailscale"],
                }
            ]
        )

        project.refresh_from_db()
        self.assertEqual(project.technologies_used, "Django, PostgreSQL")
        self.assertNotIn("projects_tech_backfilled", stats)

    def test_task_doc_imports_with_its_own_status_lifecycle(self):
        # A task carries open/active/parked/done/wontfix — the standard status
        # set rejects these, so the importer must validate tasks per-doc-type
        # (the bug that wedged `hq sync`: every open task failed validation).
        stats = import_manifest_data(
            [
                {
                    "doc_id": "task-ship-the-thing",
                    "title": "Ship the thing",
                    "doc_type": "task",
                    "status": "open",
                    "related_projects": [],
                }
            ]
        )
        self.assertEqual(stats["created"], 1)
        record = DocumentationRecord.objects.get(doc_id="task-ship-the-thing")
        self.assertEqual(record.status, "open")

    def test_task_status_set_is_distinct_from_standard_docs(self):
        # "parked" is a task status, never a standard-doc status; "deprecated" is
        # the reverse. Each doc_type is held to its own vocabulary.
        with self.assertRaises(ManifestImportError):
            import_manifest_data(
                [{"doc_id": "rb-x", "title": "x", "doc_type": "runbook", "status": "parked"}]
            )
        with self.assertRaises(ManifestImportError):
            import_manifest_data(
                [{"doc_id": "task-x", "title": "x", "doc_type": "task", "status": "deprecated"}]
            )

    def test_validate_manifest_data_flags_bad_enums_without_db(self):
        # The read-only preflight catches the contract-drift class (an invalid
        # status/doc_type/environment) before it reaches the deployed importer,
        # and writes nothing.
        problems = validate_manifest_data([
            {"doc_id": "rb-ok", "title": "ok", "doc_type": "runbook", "status": "active"},
            {"doc_id": "task-bad", "title": "bad", "doc_type": "task", "status": "deprecated"},
            {"doc_id": "rb-bad-env", "doc_type": "runbook", "environment": "nope"},
        ])
        by = {p["doc_id"]: p for p in problems}
        self.assertNotIn("rb-ok", by)
        self.assertIn("task-bad", by)
        self.assertIn("rb-bad-env", by)
        self.assertEqual(DocumentationRecord.objects.count(), 0)

    def test_check_only_command_gates_an_invalid_manifest(self):
        bad = json.dumps([{"doc_id": "task-x", "title": "x", "doc_type": "task", "status": "deprecated"}])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(bad)
            bad_path = f.name
        ok = json.dumps([{"doc_id": "task-x", "title": "x", "doc_type": "task", "status": "open"}])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(ok)
            ok_path = f.name
        try:
            with self.assertRaises(CommandError):
                call_command("import_docs_manifest", bad_path, "--check-only", stdout=StringIO())
            call_command("import_docs_manifest", ok_path, "--check-only", stdout=StringIO())  # no raise
            self.assertEqual(DocumentationRecord.objects.count(), 0)  # read-only either way
        finally:
            Path(bad_path).unlink()
            Path(ok_path).unlink()

    def test_public_article_content_item_uses_manifest_slug(self):
        import_manifest_data(
            [
                {
                    "doc_id": "writeup-custom-mcp-layer",
                    "slug": "building-a-custom-mcp-layer",
                    "title": "Building a Custom MCP Layer",
                    "doc_type": DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
                    "system": "jseverino.com",
                    "environment": DocumentationRecord.Environment.CLOUDFLARE,
                    "status": DocumentationRecord.Status.DRAFT,
                    "sensitivity": DocumentationRecord.Sensitivity.INTERNAL,
                    "content_type": "portfolio_article",
                    "published": False,
                    "path": "05 Writeups/building-a-custom-mcp-layer/index.md",
                }
            ]
        )

        record = DocumentationRecord.objects.get(doc_id="writeup-custom-mcp-layer")
        item = ContentItem.objects.get(slug="building-a-custom-mcp-layer")

        self.assertTrue(item.related_documentation.filter(pk=record.pk).exists())
        self.assertFalse(ContentItem.objects.filter(slug="custom-mcp-layer").exists())

    def test_public_article_without_content_type_prunes_legacy_content_item(self):
        record = DocumentationRecord.objects.create(
            doc_id="report-platform-playbook-public",
            title="Severino Labs Platform Playbook",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
        )
        stale_item = ContentItem.objects.create(
            slug="report-platform-playbook-public",
            title="Severino Labs Platform Playbook",
            content_type=ContentItem.Type.PORTFOLIO_PAGE,
            status=ContentItem.Status.DRAFT,
        )
        stale_item.related_documentation.add(record)

        stats = import_manifest_data(
            [
                {
                    "doc_id": "report-platform-playbook-public",
                    "title": "Severino Labs Platform Playbook",
                    "doc_type": DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
                    "system": "Severino Labs (cross-cutting)",
                    "environment": DocumentationRecord.Environment.OTHER,
                    "status": DocumentationRecord.Status.ACTIVE,
                    "sensitivity": DocumentationRecord.Sensitivity.INTERNAL,
                    "path": "02 Infrastructure/00 Reporting/Severino Labs Platform Playbook.md",
                }
            ]
        )

        self.assertEqual(stats["content_items_pruned"], 1)
        self.assertFalse(
            ContentItem.objects.filter(slug="report-platform-playbook-public").exists()
        )

    def test_prune_removes_content_item_only_linked_to_orphan_doc(self):
        old_record = DocumentationRecord.objects.create(
            doc_id="writeup-old-slug",
            title="Old title",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
        )
        stale_item = ContentItem.objects.create(
            slug="old-slug",
            title="Old title",
            content_type=ContentItem.Type.PORTFOLIO_PAGE,
        )
        stale_item.related_documentation.add(old_record)

        stats = import_manifest_data(
            [],
            report_orphans=True,
            prune_orphans=True,
        )

        self.assertEqual(stats["orphans_pruned"], 1)
        self.assertEqual(stats["content_items_pruned"], 1)
        self.assertFalse(ContentItem.objects.filter(slug="old-slug").exists())


class DeductibleMathTests(TestCase):
    def test_expense_deductible_is_recomputed_on_save(self):
        e = Expense.objects.create(
            date=date.today(),
            vendor="x",
            item="y",
            category="hosting",
            total_cost=Decimal("100.00"),
            business_use_percentage=75,
        )
        self.assertEqual(e.estimated_deductible_amount, Decimal("75.00"))
        e.total_cost = Decimal("200.00")
        e.save()
        self.assertEqual(e.estimated_deductible_amount, Decimal("150.00"))

    def test_asset_deductible_clamps_percentage(self):
        a = Asset.objects.create(
            item_name="x",
            total_cost=Decimal("100.00"),
            business_use_percentage=250,  # nonsense
        )
        # Saved value is clamped to 100.
        self.assertEqual(a.business_use_percentage, 100)
        self.assertEqual(a.estimated_deductible_amount, Decimal("100.00"))


class AuditLogTests(TestCase):
    def test_create_writes_audit_event(self):
        before = AuditLog.objects.count()
        Project.objects.create(name="audited", status=Project.Status.IDEA)
        # Audit signals attribute to no user when called outside a request,
        # but the event itself should be written.
        self.assertEqual(AuditLog.objects.count(), before + 1)
        event = AuditLog.objects.first()
        self.assertEqual(event.object_type, "Project")
        self.assertEqual(event.action, AuditLog.Action.CREATED)


class ReceiptFileProtectionTests(_AuthedTestCase):
    def _make_receipt(self) -> Receipt:
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            r = Receipt.objects.create(
                vendor="Vendor",
                date=date.today(),
                amount=Decimal("10.00"),
                original_filename="test.txt",
                content_type="text/plain",
                size_bytes=5,
            )
            # Write a file directly to the storage path so the streaming view
            # has something to serve.
            path = Path(r.file.storage.location) / "receipts" / "test.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("hello", encoding="utf-8")
            r.file.name = "receipts/test.txt"
            r.save(update_fields=["file"])
            return r

    def test_anonymous_cannot_fetch_receipt(self):
        receipt = self._make_receipt()
        self.client.logout()
        r = self.client.get(f"/receipts/{receipt.pk}/file/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])


class AuditRegistryCommandTests(TestCase):
    """`manage.py audit_registry` reports registry rows no doc references."""

    def test_reports_orphans_and_clean_state(self):
        referenced = Project.objects.create(slug="referenced", name="Referenced")
        Project.objects.create(slug="orphan-proj", name="Orphan Project")
        Asset.objects.create(slug="orphan-asset", item_name="Orphan Asset")
        doc = DocumentationRecord.objects.create(doc_id="rb-x", title="X")
        doc.related_projects.add(referenced)

        out = StringIO()
        call_command("audit_registry", "--json", stdout=out)
        stats = json.loads(out.getvalue())

        self.assertEqual(stats["orphan_projects"], ["orphan-proj"])
        self.assertEqual(stats["orphan_assets"], ["orphan-asset"])
        self.assertEqual(stats["projects_total"], 2)
        self.assertEqual(stats["assets_total"], 1)

        # Human output names the orphans and does not claim "ok".
        human = StringIO()
        call_command("audit_registry", stdout=human)
        text = human.getvalue()
        self.assertIn("orphan: orphan-proj", text)
        self.assertNotIn("Registry  ok", text)

    def test_clean_when_everything_referenced(self):
        project = Project.objects.create(slug="p", name="P")
        doc = DocumentationRecord.objects.create(doc_id="rb-y", title="Y")
        doc.related_projects.add(project)

        out = StringIO()
        call_command("audit_registry", stdout=out)
        self.assertIn("Registry  ok", out.getvalue())
