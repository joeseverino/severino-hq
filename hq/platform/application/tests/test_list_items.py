"""A record's list item is the record a save or a fetch answers, from one serializer."""

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase, TestCase, tag

from hq.domains.assets.models import Asset
from hq.domains.receipts.models import Receipt

from ..domains import records_of
from ..expenses import ExpenseCommand, save_expense
from ..read_models import list_expenses, list_receipts
from ..receipts import ReceiptMetadataCommand, update_receipt
from ..security import Principal

APPLICATION = Path(__file__).resolve().parents[1]


def _writer(resource: str) -> Principal:
    write = records_of(resource).write
    return Principal("example-writer", "test", frozenset({write}))


class ListItemIsTheSavedRecordTests(TestCase):
    def test_an_expense_lists_as_it_saved(self):
        Asset.objects.create(item_name="Example Switch", slug="example-switch")
        saved = save_expense(
            ExpenseCommand(
                date=date(2030, 1, 2),
                vendor="Example Host",
                item="Hosting",
                total_cost=Decimal("12.00"),
                payment_method="credit",
                notes="Example note",
                related_asset="example-switch",
            ),
            principal=_writer("expenses"),
        )["expense"]

        self.assertEqual(list_expenses()["items"], [saved])

    def test_a_receipt_lists_as_it_saved(self):
        receipt = Receipt.objects.create(file="receipts/example.pdf", content_type="application/pdf")
        saved = update_receipt(
            ReceiptMetadataCommand(vendor="Example Host", date=date(2030, 1, 2), notes="Example note"),
            principal=_writer("receipts"),
            current_id=receipt.pk,
        )["receipt"]

        self.assertEqual(list_receipts()["items"], [saved])

    def test_the_filters_still_narrow(self):
        writer = _writer("expenses")
        for day, category in ((date(2030, 1, 2), "software"), (date(2031, 1, 2), "hardware")):
            save_expense(ExpenseCommand(date=day, vendor="Example", item="Item", category=category), principal=writer)
        matched = Receipt.objects.create(file="receipts/a.pdf", related_asset=Asset.objects.create(slug="example-a"))
        Receipt.objects.create(file="receipts/b.pdf")

        self.assertEqual([item["date"] for item in list_expenses(year=2030)["items"]], ["2030-01-02"])
        self.assertEqual([item["category"] for item in list_expenses(category="hardware")["items"]], ["hardware"])
        self.assertNotIn(matched.pk, [item["id"] for item in list_receipts(unmatched_only=True)["items"]])


def _functions(prefix: str):
    for path in sorted(APPLICATION.glob("*.py")):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith(prefix):
                yield path, node


def _serializers() -> dict[str, str]:
    """Each model a serializer takes, by class name, and that serializer's name."""

    found = {}
    for _, node in _functions("serialize_"):
        annotation = node.args.args[0].annotation if node.args.args else None
        if isinstance(annotation, ast.Name) and annotation.id != "Any":
            found[annotation.id] = node.name
    return found


@tag("source")
class ListItemsComeFromTheSerializerTests(SimpleTestCase):
    def test_a_list_of_a_serialized_record_names_its_serializer(self):
        """A list that builds its own item drifts from the record a save or a
        fetch answers: a field added to one is missing from the other, and the
        same relation is spelt two ways. A ``list_*`` read that names a model
        with a serializer names that serializer too."""

        serializers = _serializers()
        held, drifted = set(), []
        for path, function in _functions("list_"):
            names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
            for model in names & serializers.keys():
                held.add(function.name)
                if serializers[model] not in names:
                    drifted.append(f"{path.name}::{function.name} builds {model} items without {serializers[model]}")

        self.assertEqual(drifted, [])
        self.assertLessEqual(
            {"list_expenses", "list_receipts", "list_assets", "list_projects"},
            held,
            "a list this rule exists for is no longer seen by it: the rule checks nothing",
        )
