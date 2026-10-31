"""An amount is written one way on every page."""

from decimal import Decimal

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq_sdk import money as sdk

from ..money import MINUS, WORD_JOINER, money, signed_number
from ..ui import MISSING
from .markup import text, with_class


class MoneyTests(SimpleTestCase):
    def test_an_amount_has_its_sign_its_separators_and_its_cents(self):
        self.assertEqual(money(Decimal("1234.5")), "$1,234.50")
        self.assertEqual(money(0), "$0.00")

    def test_a_negative_amount_takes_one_true_minus_sign_held_to_the_dollar(self):
        # The joiner is what keeps a line from ending between the sign and the figure.
        self.assertEqual(money(Decimal(-5)), f"{MINUS}{WORD_JOINER}$5.00")

    def test_a_change_is_signed_either_way_and_held_to_its_sign(self):
        self.assertEqual(money(Decimal(5), plus=True), f"+{WORD_JOINER}$5.00")
        self.assertEqual(money(Decimal(-5), cents=False, plus=True), f"{MINUS}{WORD_JOINER}$5")
        self.assertEqual(money(None, plus=True), MISSING)

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

        self.assertEqual(text(drawn), f"{money(Decimal('-1234.5'))} {money(Decimal('-1234.5'), cents=False)} {MISSING}")
        self.assertEqual([mark.words for mark in with_class(drawn, "empty-value")], [MISSING])

    def test_the_filter_refuses_a_form_it_does_not_have(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{{ a|money:'short' }}").render(Context({"a": 1}))

    def test_a_scanned_amount_that_rounds_to_nothing_is_not_negative(self):
        self.assertEqual(money(Decimal("-0.40"), cents=False), "$0")

    def test_an_extension_writes_amounts_with_the_same_function(self):
        self.assertIs(sdk.money, money)
        self.assertIs(sdk.signed_number, signed_number)


class SignedNumberTests(SimpleTestCase):
    """Any figure takes its sign by the rule an amount does."""

    @staticmethod
    def beats(value: int) -> str:
        return f"{value} bpm"

    @staticmethod
    def tenths(value: float) -> str:
        return f"{value:.1f} min"

    def test_a_negative_figure_takes_one_true_minus_sign_held_to_it(self):
        self.assertEqual(signed_number(-29, self.beats), f"{MINUS}{WORD_JOINER}29 bpm")

    def test_a_positive_figure_is_signed_only_as_a_change(self):
        self.assertEqual(signed_number(29, self.beats), "29 bpm")
        self.assertEqual(signed_number(1.25, self.tenths, plus=True), f"+{WORD_JOINER}1.2 min")

    def test_a_figure_that_writes_as_zero_is_not_negative(self):
        self.assertEqual(signed_number(-0.04, self.tenths), "0.0 min")
        self.assertEqual(signed_number(-0.04, self.tenths, plus=True), f"+{WORD_JOINER}0.0 min")

    def test_a_missing_value_is_the_mark_for_one(self):
        self.assertEqual(signed_number(None, self.beats), MISSING)
