from decimal import Decimal

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase


def drawn(source: str, **context) -> str:
    return Template("{% load value_tags %}" + source).render(Context(context))


class MeasuredTests(SimpleTestCase):
    """A figure with its unit, rounded where it is written, the same in a
    template as in Python."""

    def test_the_filter_rounds_to_the_decimals_it_is_given(self):
        for source, value, written in (
            ('{{ x|measured:"mi,2" }}', 3.5712, "3.57\xa0mi"),
            # Half away from zero, as floatformat rounds, never to the even digit.
            ('{{ x|measured:"mi,2" }}', 2.675, "2.68\xa0mi"),
            ('{{ x|measured:"lb,0" }}', 1234.5, "1,235\xa0lb"),
            # A negative count gives decimals only to a figure that is not whole.
            ('{{ x|measured:"CPUs,-2" }}', 2.0, "2\xa0CPUs"),
            ('{{ x|measured:"CPUs,-2" }}', 2.5, "2.50\xa0CPUs"),
            ('{{ x|measured:"more" }}', 1200, "1,200\xa0more"),
        ):
            with self.subTest(source=source, value=value):
                self.assertEqual(drawn(source, x=value), written)

    def test_zero_is_a_figure_and_only_nothing_is_the_missing_mark(self):
        self.assertEqual(drawn('{{ x|measured:"mi,1" }}', x=0), "0.0\xa0mi")
        self.assertEqual(drawn('{{ x|measured:"more" }}', x=0), "0\xa0more")
        for missing in (None, ""):
            with self.subTest(missing=missing):
                shown = drawn('{{ x|measured:"mi,1" }}', x=missing)
                self.assertIn('class="empty-value"', shown)
                self.assertNotIn("mi", shown)

    def test_a_percent_stands_against_its_figure(self):
        self.assertEqual(drawn('{{ x|measured:"%,-1" }}', x=12.5), "12.5%")

    def test_a_figure_that_rounds_to_zero_carries_no_sign(self):
        from hq.platform.application.ui import measured

        self.assertEqual(measured(-0.04, "min", 1), "0.0 min")
        self.assertEqual(measured(-3.2, "bpm", 0), "−⁠3 bpm")
        self.assertEqual(measured(1.25, "min", 1, plus=True), "+⁠1.3 min")

    def test_python_and_the_template_write_one_figure(self):
        from hq_sdk.ui import measured

        for value, unit, places in ((4.256, "mi", 2), (Decimal("1234.50"), "ft", None), (0, "s", None)):
            with self.subTest(value=value):
                argument = unit if places is None else f"{unit},{places}"
                self.assertEqual(
                    drawn("{{ x|measured:unit }}", x=value, unit=argument).replace("\xa0", " "),
                    measured(value, unit, places),
                )

    def test_a_figure_already_in_words_is_written_as_it_came(self):
        self.assertEqual(drawn('{{ x|measured:"measured" }}', x="1:23"), "1:23\xa0measured")

    def test_a_count_of_decimals_that_is_not_a_number_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            drawn('{{ x|measured:"mi,two" }}', x=1)
