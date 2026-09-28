import csv
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from . import exports


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
        call_command("seed_demo", verbosity=0)

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
        self.assertTrue(any(record[related] for record in records))
        self.assertFalse(any(record[related].isdigit() for record in records))
