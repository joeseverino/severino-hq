"""The business-use range holds on every interface, not only the web forms.

The API and MCP once accepted 150 where the browser refused it, and the model
quietly stored 100. One range now lives in ``application.money``: the command
type publishes it and every save enforces it.
"""

from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase

from assets.models import Asset
from expenses.models import Expense

from .assets import AssetCommand, save_asset
from .capabilities import capability_registry, execute_capability
from .expenses import ExpenseCommand, save_expense
from .integration_specs import command_schema
from .money import BUSINESS_USE_MAX, BUSINESS_USE_MIN
from .security import Capability, Principal

OPERATOR = Principal("test", "operator", frozenset(Capability))
TOO_MUCH = BUSINESS_USE_MAX + 50


class ApiRefusalTests(TestCase):
    def test_an_asset_over_the_range_is_refused_by_field(self):
        result = execute_capability(
            "asset.create",
            {"item_name": "Example laptop", "business_use_percentage": TOO_MUCH},
            principal=OPERATOR,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertIn("business_use_percentage", str(result["error"]))
        self.assertFalse(Asset.objects.exists())

    def test_an_expense_under_the_range_is_refused_by_field(self):
        result = execute_capability(
            "expense.create",
            {
                "date": "2026-01-02",
                "vendor": "Example",
                "item": "Example item",
                "business_use_percentage": BUSINESS_USE_MIN - 1,
            },
            principal=OPERATOR,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(Expense.objects.exists())

    def test_a_percentage_in_range_is_stored_as_sent(self):
        result = execute_capability(
            "asset.create",
            {"item_name": "Example desk", "business_use_percentage": 40},
            principal=OPERATOR,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(Asset.objects.get().business_use_percentage, 40)

    def test_the_published_schema_states_the_range(self):
        for name in ("asset.create", "expense.create"):
            with self.subTest(capability=name):
                schema = command_schema(capability_registry()[name].command_type)
                field = schema["properties"]["business_use_percentage"]

                self.assertEqual(field["minimum"], BUSINESS_USE_MIN)
                self.assertEqual(field["maximum"], BUSINESS_USE_MAX)


class ServiceRefusalTests(TestCase):
    """What the CLI and any direct caller reach, without the API's parsing."""

    def test_saving_an_asset_over_the_range_raises_on_the_field(self):
        command = AssetCommand(item_name="Example laptop", business_use_percentage=TOO_MUCH)

        with self.assertRaises(ValidationError) as caught:
            save_asset(command, principal=OPERATOR)

        self.assertIn("business_use_percentage", caught.exception.message_dict)
        self.assertFalse(Asset.objects.exists())

    def test_saving_an_expense_over_the_range_raises_on_the_field(self):
        command = ExpenseCommand(
            date=date(2026, 1, 2),
            vendor="Example",
            item="Example item",
            business_use_percentage=TOO_MUCH,
        )

        with self.assertRaises(ValidationError) as caught:
            save_expense(command, principal=OPERATOR)

        self.assertIn("business_use_percentage", caught.exception.message_dict)
        self.assertFalse(Expense.objects.exists())
