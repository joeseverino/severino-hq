"""The manifest importer and the export it reads back."""

import json
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.docs_index.importer import ManifestImportError, import_manifest_data, validate_manifest_data
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.tests.markup import text
from hq.platform.application.tests.writers import form_keyed

from .signed_in_case import _AuthedTestCase


class ManifestUploadTests(_AuthedTestCase):
    def upload(self, content):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(
            "/docs/import/",
            form_keyed({"manifest_file": SimpleUploadedFile("manifest.json", content, "application/json")}),
        )

    def test_a_file_that_is_not_utf8_is_refused_with_a_reason(self):
        response = self.upload(b"\xff\xfe[]")

        self.assertEqual(response.status_code, 200)
        self.assertIn("not UTF-8", text(response))

    def test_an_oversized_manifest_is_refused_before_it_is_read(self):
        from hq.domains.docs_index.forms import MAX_MANIFEST_BYTES

        with patch("hq.domains.docs_index.forms.MAX_MANIFEST_BYTES", 8):
            response = self.upload(b"[" + b" " * 16 + b"]")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A manifest is at most")
        self.assertGreater(MAX_MANIFEST_BYTES, 8)


class ExportSmokeTests(_AuthedTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(name="Lab: homelab DNS", status=Project.Status.ACTIVE)
        self.asset = Asset.objects.create(
            item_name="Test switch",
            total_cost=Decimal("29.00"),
            business_use_percentage=100,
            purchase_date=timezone.localdate(),
            status=Asset.Status.ACTIVE,
        )
        self.expense = Expense.objects.create(
            date=timezone.localdate(),
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
        self.content = ContentItem.objects.create(title="Test article", status=ContentItem.Status.DRAFT)

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
                    "system": "Example service",
                    "environment": "other",
                    "sensitivity": "internal",
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
                    "system": "Example service",
                    "environment": "other",
                    "sensitivity": "internal",
                    "related_projects": [project.slug],
                    "tags": ["sqlite", "tailscale"],
                }
            ]
        )

        project.refresh_from_db()
        self.assertEqual(project.technologies_used, "Django, PostgreSQL")
        self.assertNotIn("projects_tech_backfilled", stats)

    def test_a_task_note_is_not_a_doc_the_vault_writes(self):
        # The vault keeps no task notes: their type and lifecycle are refused.
        for case, entry in (
            ("task type", {**self.STANDARD_DOC, "doc_id": "rb-x", "doc_type": "task"}),
            ("task status", {**self.STANDARD_DOC, "doc_id": "rb-x", "status": "parked"}),
        ):
            with self.subTest(case), self.assertRaises(ManifestImportError):
                import_manifest_data([entry])
        self.assertEqual(DocumentationRecord.objects.count(), 0)

    def test_validate_manifest_data_flags_bad_enums_without_db(self):
        # The read-only preflight catches the contract-drift class (an invalid
        # status/doc_type/environment) before it reaches the deployed importer,
        # and writes nothing.
        problems = validate_manifest_data(
            [
                {
                    "doc_id": "rb-ok",
                    "title": "ok",
                    "doc_type": "runbook",
                    "status": "active",
                    "system": "Example service",
                    "environment": "other",
                    "sensitivity": "internal",
                },
                {
                    "doc_id": "rb-bad-status",
                    "title": "bad",
                    "doc_type": "runbook",
                    "status": "open",
                    "system": "Example service",
                    "environment": "other",
                    "sensitivity": "internal",
                },
                {"doc_id": "rb-bad-env", "doc_type": "runbook", "environment": "nope"},
            ]
        )
        by = {p["doc_id"]: p for p in problems}
        self.assertNotIn("rb-ok", by)
        self.assertIn("rb-bad-status", by)
        self.assertIn("rb-bad-env", by)
        self.assertEqual(DocumentationRecord.objects.count(), 0)

    # An entry breaking the schema's prefixes or required fields is refused,
    # not upserted. Each rejection is asserted on
    # both paths (the preflight and the write) since they share one check.
    STANDARD_DOC = {
        "doc_id": "rb-complete",
        "title": "Complete",
        "doc_type": "runbook",
        "system": "Example service",
        "environment": "other",
        "status": "active",
        "sensitivity": "internal",
    }

    def _assert_rejected(self, entry, *fragments):
        problems = validate_manifest_data([entry])
        self.assertEqual(len(problems), 1, problems)
        self.assertEqual(problems[0]["doc_id"], entry["doc_id"])
        message = problems[0]["errors"][0]
        for fragment in (entry["doc_id"], *fragments):
            self.assertIn(fragment, message)
        with self.assertRaisesRegex(ManifestImportError, entry["doc_id"]):
            import_manifest_data([entry])
        self.assertEqual(DocumentationRecord.objects.count(), 0)

    CONTENT_DOC = {
        **STANDARD_DOC,
        "doc_id": "writeup-complete",
        "doc_type": "public_article_draft",
        "content_type": "portfolio_article",
        "slug": "complete",
        "published": False,
    }

    def test_an_entry_off_the_schema_is_refused_by_the_preflight_and_the_write(self):
        def without(entry, *fields):
            return {key: value for key, value in entry.items() if key not in fields}

        for case, entry, names in (
            ("prefix", {**self.STANDARD_DOC, "doc_id": "wiki-complete"}, ("rb-",)),
            ("missing", without(self.STANDARD_DOC, "environment", "sensitivity"), ("environment", "sensitivity")),
            ("blank", {**self.STANDARD_DOC, "system": ""}, ("system",)),
            # The schema's prefixes bind every vault doc: a runbook cannot pass with a writeup's id.
            ("vault doc with a content id", {**self.STANDARD_DOC, "doc_id": "writeup-complete"}, ()),
            # Not routed to ContentItem, so held to the schema's prefixes.
            ("article without a content type", without(self.CONTENT_DOC, "content_type"), ()),
            ("content missing", without(self.CONTENT_DOC, "system"), ("system",)),
            ("content without its item", without(self.CONTENT_DOC, "slug", "published"), ("slug", "published")),
            ("content with a blank slug", {**self.CONTENT_DOC, "slug": ""}, ("slug",)),
        ):
            with self.subTest(case):
                self._assert_rejected(entry, *names)

    def test_content_entry_id_is_not_held_to_the_doc_prefixes(self):
        for doc_id in ("writeup-complete", "page-about", "anything-at-all"):
            with self.subTest(doc_id=doc_id):
                entry = {**self.CONTENT_DOC, "doc_id": doc_id}
                self.assertEqual(validate_manifest_data([entry]), [])

    def test_valid_standard_doc_passes(self):
        manifest = [self.STANDARD_DOC]
        self.assertEqual(validate_manifest_data(manifest), [])
        stats = import_manifest_data(manifest)
        self.assertEqual(stats["created"], 1)

    def test_check_only_command_gates_an_invalid_manifest(self):
        bad = json.dumps([{**self.STANDARD_DOC, "status": "open"}])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(bad)
            bad_path = f.name
        ok = json.dumps([self.STANDARD_DOC])
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
                    "doc_id": "writeup-example-article",
                    "slug": "writing-an-example-article",
                    "title": "Writing an Example Article",
                    "doc_type": DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
                    "system": "example.com",
                    "environment": DocumentationRecord.Environment.CLOUDFLARE,
                    "status": DocumentationRecord.Status.DRAFT,
                    "sensitivity": DocumentationRecord.Sensitivity.INTERNAL,
                    "content_type": "portfolio_article",
                    "published": False,
                    "path": "example/writing-an-example-article/index.md",
                }
            ]
        )

        record = DocumentationRecord.objects.get(doc_id="writeup-example-article")
        item = ContentItem.objects.get(slug="writing-an-example-article")

        self.assertTrue(item.related_documentation.filter(pk=record.pk).exists())
        self.assertFalse(ContentItem.objects.filter(slug="example-article").exists())

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
