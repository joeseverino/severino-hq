"""An amount is written one way on every page."""

from decimal import Decimal

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import text

from ..money import money
from ..ui import MISSING


class MoneyTests(SimpleTestCase):
    def test_a_missing_amount_is_the_mark_for_one(self):
        self.assertEqual(money(None), MISSING)
        self.assertEqual(money("not a number"), MISSING)

    def test_the_filter_writes_what_the_function_writes(self):
        drawn = Template("{{ a|money }} {{ a|money:'whole' }} {{ nothing|money }}").render(
            Context({"a": Decimal("-1234.5"), "nothing": None})
        )

        self.assertEqual(text(drawn), f"{money(Decimal('-1234.5'))} {money(Decimal('-1234.5'), cents=False)} {MISSING}")

    def test_the_filter_refuses_a_form_it_does_not_have(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{{ a|money:'short' }}").render(Context({"a": 1}))
