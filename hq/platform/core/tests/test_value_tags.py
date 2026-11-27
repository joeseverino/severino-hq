"""What the value filters promise that no drawn example can show: words a
page gives are drawn as text, and a figure is rounded where it is written,
the same in a template as in Python."""

from decimal import Decimal

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import select
from hq.platform.core.templatetags.value_tags import breakable, cell_line, code_spans, readout

# Words a page gives, drawn as markup only where a filter fails to escape them.
HOSTILE = "<i data-hostile></i>"


def drawn(source: str, **context) -> str:
    return Template("{% load value_tags %}" + source).render(Context(context))


class WordsGivenAreTextTests(SimpleTestCase):
    """EveryPartTests holds this for each part's options; these filters draw
    words outside any part."""

    def test_markup_given_to_a_value_filter_is_drawn_as_text(self):
        for written in (
            breakable(f"{HOSTILE}.example.com"),
            breakable(f"restart {HOSTILE}-web now", "·"),
            readout(f"{HOSTILE}.example.com:443"),
            cell_line(f"{HOSTILE} · example-web", "/example/"),
            code_spans(f"{HOSTILE} and `{HOSTILE}` then `x`{HOSTILE}"),
        ):
            with self.subTest(written):
                self.assertEqual(select(str(written), "data-hostile"), [])


class MeasuredTests(SimpleTestCase):
    def test_a_figure_is_rounded_half_away_from_zero_and_zero_carries_no_sign(self):
        from hq.platform.application.ui import measured

        for value, unit, places, written in (
            (3.5712, "mi", 2, "3.57 mi"),
            # Half away from zero, as floatformat rounds, never to the even digit.
            (2.675, "mi", 2, "2.68 mi"),
            (1234.5, "lb", 0, "1,235 lb"),
            # A negative count gives decimals only to a figure that is not whole.
            (2.0, "CPUs", -2, "2 CPUs"),
            (2.5, "CPUs", -2, "2.50 CPUs"),
            (0, "mi", 1, "0.0 mi"),
            (-0.04, "min", 1, "0.0 min"),
            (-0.04, "mi", -1, "0.0 mi"),
        ):
            with self.subTest(value=value, unit=unit, places=places):
                self.assertEqual(measured(value, unit, places), written)

    def test_python_and_the_template_write_one_figure(self):
        from hq_sdk.ui import measured

        for value, unit, places in (
            (4.256, "mi", 2),
            (Decimal("1234.50"), "ft", None),
            (0, "s", None),
            (2.675, "mi", 2),
        ):
            with self.subTest(value=value):
                argument = unit if places is None else f"{unit},{places}"
                self.assertEqual(
                    drawn("{{ x|measured:unit }}", x=value, unit=argument).replace("\xa0", " "),
                    measured(value, unit, places),
                )
