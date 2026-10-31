import csv
import json
from decimal import Decimal
from io import StringIO

from django.test import TestCase

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.core import example_estate
from hq.platform.core.example_estate.testing import throwaway_media

from .. import exports


class CsvExportTests(TestCase):
    EXPORTS = {
        "expenses_csv": exports.EXPENSE_COLUMNS,
        "assets_csv": exports.ASSET_COLUMNS,
        "content_csv": exports.CONTENT_COLUMNS,
        "projects_csv": exports.PROJECT_COLUMNS,
        "documentation_csv": exports.DOCUMENTATION_COLUMNS,
    }

    @classmethod
    def setUpTestData(cls):
        with throwaway_media():
            example_estate.seed()

    def rows(self, name):
        return list(csv.reader(StringIO(getattr(exports, name)())))

    def test_the_header_is_the_declared_columns(self):
        for name, columns in self.EXPORTS.items():
            with self.subTest(export=name):
                header = self.rows(name)[0]
                self.assertEqual(
                    header,
                    [column if isinstance(column, str) else column[0] for column in columns],
                )

    def test_every_row_has_a_cell_for_every_column(self):
        for name in self.EXPORTS:
            with self.subTest(export=name):
                header, *records = self.rows(name)
                self.assertTrue(records, "the demo estate should export something")
                self.assertEqual({len(record) for record in records}, {len(header)})

    def test_a_related_record_is_exported_by_its_handle(self):
        header, *records = self.rows("expenses_csv")
        related = header.index("related_project")
        handles = sorted(
            Expense.objects.filter(related_project__isnull=False).values_list("related_project__slug", flat=True)
        )
        self.assertTrue(handles, "the estate links no expense to a project")
        self.assertEqual(sorted(record[related] for record in records if record[related]), handles)


class YearSummaryTests(TestCase):
    def test_a_record_names_what_it_relates_to_by_its_handle(self):
        project = Project.objects.create(name="Example project", slug="example-project", technologies_used="Go, SQL")
        doc = DocumentationRecord.objects.create(doc_id="rb-example", title="Example runbook")
        doc.related_projects.add(project)
        post = ContentItem.objects.create(title="Example post", slug="example-post", tags="ops, dns")
        post.related_projects.add(project)
        post.related_documentation.add(doc)
        asset = Asset.objects.create(item_name="Example switch", slug="example-switch", purchase_date="2030-05-01")
        asset.related_projects.add(project)
        post.related_assets.add(asset)

        summary = json.loads(exports.year_summary_json(2030))

        (exported_project,) = summary["projects"]
        (exported_post,) = summary["content"]
        (exported_doc,) = summary["documentation"]
        (exported_asset,) = summary["assets"]
        self.assertEqual(exported_project["technologies"], ["Go", "SQL"])
        self.assertEqual(exported_post["tags"], ["ops", "dns"])
        self.assertEqual(
            (exported_post["related_projects"], exported_post["related_assets"]),
            (["example-project"], ["example-switch"]),
        )
        self.assertEqual(exported_post["related_documentation"], ["rb-example"])
        self.assertEqual((exported_doc["related_projects"], exported_doc["related_assets"]), (["example-project"], []))
        self.assertEqual(exported_asset["related_projects"], ["example-project"])
        self.assertEqual(
            (exported_asset["total_cost"], exported_asset["estimated_deductible_amount"]), ("0.00", "0.00")
        )

    def test_a_year_counts_only_the_expenses_and_assets_it_holds(self):
        expense = {"vendor": "Example Host", "item": "Hosting", "business_use_percentage": 50}
        Expense.objects.create(date="2030-01-02", category="hosting", total_cost=Decimal("12.00"), **expense)
        Expense.objects.create(date="2030-03-02", category="domains", total_cost=Decimal("30.00"), **expense)
        Expense.objects.create(date="2031-01-02", category="hosting", total_cost=Decimal("99.00"), **expense)
        Asset.objects.create(item_name="Example switch", purchase_date="2030-05-01", total_cost=Decimal("80.00"))
        Asset.objects.create(item_name="Example router", purchase_date="2031-05-01", total_cost=Decimal("70.00"))

        summary = exports.year_summary(2030)

        totals = summary["totals"]
        self.assertEqual((totals["expenses_count"], totals["expenses_total"]), (2, "42.00"))
        self.assertEqual(totals["expenses_deductible_total"], "21.00")
        self.assertEqual((totals["assets_count"], totals["assets_total"]), (1, "80.00"))
        self.assertEqual([row["category"] for row in summary["expenses_by_category"]], ["domains", "hosting"])
        self.assertEqual([row["total_cost"] for row in summary["largest_expenses"]], ["30.00", "12.00"])
        self.assertEqual([asset["item_name"] for asset in summary["assets"]], ["Example switch"])
        self.assertEqual(summary["assets"][0]["purchase_date"], "2030-05-01")

    def test_a_year_with_nothing_in_it_totals_zero(self):
        Expense.objects.create(date="2031-01-02", vendor="Example Host", item="Hosting", total_cost=Decimal("9.00"))

        summary = exports.year_summary(2030)

        self.assertEqual(
            summary["totals"],
            {
                "expenses_count": 0,
                "expenses_total": "0.00",
                "expenses_deductible_total": "0.00",
                "assets_count": 0,
                "assets_total": "0.00",
                "assets_deductible_total": "0.00",
            },
        )
        self.assertEqual(
            (summary["expenses_by_category"], summary["largest_expenses"], summary["assets"]), ([], [], [])
        )

    def test_a_date_not_yet_set_reads_as_none(self):
        ContentItem.objects.create(title="Published post", published_at="2030-02-03")
        ContentItem.objects.create(title="Draft post")
        DocumentationRecord.objects.create(doc_id="rb-reviewed", title="Reviewed", last_reviewed="2030-04-05")
        DocumentationRecord.objects.create(doc_id="rb-unreviewed", title="Unreviewed")

        summary = exports.year_summary(2030)

        published = {item["title"]: item["published_at"] for item in summary["content"]}
        reviewed = {doc["doc_id"]: doc["last_reviewed"] for doc in summary["documentation"]}
        self.assertEqual(published, {"Published post": "2030-02-03", "Draft post": None})
        self.assertEqual(reviewed, {"rb-reviewed": "2030-04-05", "rb-unreviewed": None})
