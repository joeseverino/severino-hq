"""An amount is written one way on every page."""

from decimal import Decimal

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq_sdk import money as sdk

from ..money import MINUS, money
from ..ui import MISSING


class MoneyTests(SimpleTestCase):
    def test_an_amount_has_its_sign_its_separators_and_its_cents(self):
        self.assertEqual(money(Decimal("1234.5")), "$1,234.50")
        self.assertEqual(money(0), "$0.00")

    def test_a_negative_amount_takes_one_true_minus_sign_before_the_dollar(self):
        self.assertEqual(money(Decimal(-5)), f"{MINUS}$5.00")

    def test_an_amount_that_rounds_to_nothing_is_not_negative(self):
        self.assertEqual(money(Decimal("-0.004")), "$0.00")

    def test_a_scanned_figure_leaves_its_cents_off(self):
        self.assertEqual(money(Decimal("1234.56"), cents=False), "$1,235")

    def test_a_stored_text_amount_reads_as_its_number(self):
        self.assertEqual(money("12.10"), "$12.10")

    def test_a_missing_amount_is_the_mark_for_one(self):
        self.assertEqual(money(None), MISSING)
        self.assertEqual(money("not a number"), MISSING)

    def test_the_filter_writes_what_the_function_writes(self):
        drawn = Template("{{ a|money }} {{ a|money:'whole' }} {{ nothing|money }}").render(
            Context({"a": Decimal("-1234.5"), "nothing": None})
        )

        self.assertEqual(drawn, f"{MINUS}$1,234.50 {MINUS}$1,234 " + '<span class="empty-value" title="None">' + MISSING + "</span>")

    def test_the_filter_refuses_a_form_it_does_not_have(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{{ a|money:'short' }}").render(Context({"a": 1}))

    def test_an_extension_writes_amounts_with_the_same_function(self):
        self.assertIs(sdk.money, money)
        self.assertEqual(sdk.MINUS, MINUS)
