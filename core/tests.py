"""Smoke tests for Severino HQ.

These don't aim for exhaustive coverage — they verify that the auth gate works
and every page in the main nav (including reports + exports) renders without a
500. Add module-specific tests inside each app as it grows.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from assets.models import Asset
from content.models import ContentItem
from core.models import AuditLog
from docs_index.models import DocumentationRecord
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
