"""The business-use range is one rule, declared on the model and held on every path."""

from __future__ import annotations

import json
from datetime import date

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from assets.models import Asset
from expenses.models import Expense
from hq_api.tests import ISSUER, RESOURCE, _serving, _token
from hq_mcp.identity import reset_principal, set_principal
from hq_mcp.server import mcp

from ..assets import AssetCommand, save_asset
from ..expenses import ExpenseCommand, save_expense
from ..security import cli_principal, mcp_principal

OUT_OF_RANGE = (150, -1)
IN_RANGE = (0, 100)
EXPECTED = {150: "must be at most 100", -1: "must be at least 0"}


def _asset(pct: int, slug: str) -> dict:
    return {"item_name": "Example asset", "slug": slug, "business_use_percentage": pct}


def _expense(pct: int) -> dict:
    return {
        "date": "2026-07-25",
        "vendor": "Example vendor",
        "item": "Example item",
        "business_use_percentage": pct,
    }


class RuleTests(TestCase):
    def test_the_bounds_are_inclusive(self):
        for value in IN_RANGE:
            with self.subTest(value=value):
                Asset(item_name="Example asset", business_use_percentage=value).full_clean()

    def test_out_of_range_names_the_bound_it_broke(self):
        for value, code in ((150, "max_value"), (-1, "min_value")):
            with self.subTest(value=value), self.assertRaises(ValidationError) as caught:
                Asset(item_name="Example asset", business_use_percentage=value).full_clean()
            (error,) = caught.exception.error_dict["business_use_percentage"]
            self.assertEqual(error.code, code)

    def test_the_table_refuses_a_row_written_around_validation(self):
        for create in (
            lambda: Asset.objects.create(item_name="Example asset", business_use_percentage=150),
            lambda: Expense.objects.create(
                date=date(2026, 7, 25), vendor="V", item="I", business_use_percentage=150
            ),
        ):
            with self.subTest(create=create), self.assertRaises(IntegrityError), transaction.atomic():
                create()


class ServiceTests(TestCase):
    def test_the_services_refuse_out_of_range_and_store_nothing(self):
        for value in OUT_OF_RANGE:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as asset_error:
                    save_asset(
                        AssetCommand(**_asset(value, f"refused-{value}")),
                        principal=cli_principal(),
                    )
                with self.assertRaises(ValidationError) as expense_error:
                    save_expense(
                        ExpenseCommand(
                            date=date(2026, 7, 25),
                            vendor="Example vendor",
                            item="Example item",
                            business_use_percentage=value,
                        ),
                        principal=cli_principal(),
                    )
                for caught in (asset_error, expense_error):
                    self.assertIn("business_use_percentage", caught.exception.error_dict)
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(Expense.objects.exists())

    def test_the_bounds_are_accepted_and_stored_as_sent(self):
        for value in IN_RANGE:
            with self.subTest(value=value):
                asset = save_asset(
                    AssetCommand(**_asset(value, f"kept-{value}")),
                    principal=cli_principal(),
                )["asset"]
                expense = save_expense(
                    ExpenseCommand(
                        date=date(2026, 7, 25),
                        vendor="Example vendor",
                        item="Example item",
                        business_use_percentage=value,
                    ),
                    principal=cli_principal(),
                )["expense"]
                self.assertEqual(asset["business_use_percentage"], value)
                self.assertEqual(expense["business_use_percentage"], value)


@override_settings(SEVERINO_MCP_ENABLE_WRITES=True)
class MCPTests(TestCase):
    def _call(self, name: str, payload: dict) -> dict:
        async def call():
            tool = mcp._tool_manager.get_tool("execute_capability")
            return await tool.run({"name": name, "payload": payload})

        bound = set_principal(mcp_principal())
        try:
            return async_to_sync(call)()
        finally:
            reset_principal(bound)

    def test_out_of_range_is_invalid_input(self):
        for value in OUT_OF_RANGE:
            for name, payload in (
                ("asset.create", _asset(value, f"mcp-{value}")),
                ("expense.create", _expense(value)),
            ):
                with self.subTest(capability=name, value=value):
                    result = self._call(name, payload)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"]["code"], "invalid_input")
                    self.assertEqual(
                        result["error"]["message"],
                        f"{name}: business_use_percentage {EXPECTED[value]}.",
                    )
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(Expense.objects.exists())

    def test_the_bounds_are_accepted(self):
        for value in IN_RANGE:
            with self.subTest(value=value):
                asset = self._call("asset.create", _asset(value, f"mcp-ok-{value}"))
                expense = self._call("expense.create", _expense(value))
                self.assertTrue(asset["ok"], asset)
                self.assertTrue(expense["ok"], expense)


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class APITests(TestCase):
    TOKEN_SCOPE = "write_assets write_expenses"

    def _post(self, name: str, payload: dict, key: str):
        with _serving():
            return self.client.post(
                f"/api/v2/capabilities/{name}/",
                data=json.dumps({"payload": payload}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope=self.TOKEN_SCOPE)}",
                HTTP_IDEMPOTENCY_KEY=key,
            )

    def test_out_of_range_is_a_400_naming_the_field(self):
        for value in OUT_OF_RANGE:
            for name, payload in (
                ("asset.create", _asset(value, f"api-{value}")),
                ("expense.create", _expense(value)),
            ):
                with self.subTest(capability=name, value=value):
                    response = self._post(name, payload, f"{name}-{value}")
                    self.assertEqual(response.status_code, 400)
                    error = response.json()["error"]
                    self.assertEqual(error["code"], "invalid_input")
                    self.assertEqual(
                        error["message"],
                        f"{name}: business_use_percentage {EXPECTED[value]}.",
                    )
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(Expense.objects.exists())

    def test_the_bounds_are_accepted(self):
        for value in IN_RANGE:
            with self.subTest(value=value):
                asset = self._post("asset.create", _asset(value, f"api-ok-{value}"), f"a{value}")
                expense = self._post("expense.create", _expense(value), f"e{value}")
                self.assertEqual(asset.status_code, 200, asset.content)
                self.assertEqual(expense.status_code, 200, expense.content)
                self.assertEqual(
                    asset.json()["data"]["asset"]["business_use_percentage"], value
                )


class WebFormTests(TestCase):
    def test_the_form_still_refuses_on_the_field(self):
        user = get_user_model().objects.create_user(
            username="example-operator", password="example-password"
        )
        self.client.force_login(user)
        for value in OUT_OF_RANGE:
            with self.subTest(value=value):
                response = self.client.post(
                    reverse("assets:create"),
                    {
                        "item_name": "Example asset",
                        "slug": f"web-{value}",
                        "category": "other",
                        "total_cost": "10.00",
                        "business_use_percentage": str(value),
                        "status": "active",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "business_use_percentage", response.context["form"].errors
                )
        self.assertFalse(Asset.objects.exists())
