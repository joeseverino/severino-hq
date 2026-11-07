"""An allocation: parts of one whole drawn as one bar, with no width written by the page."""

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import elements, links, select, within
from hq_sdk.ui import AllocationBar, Share


class AllocationTests(SimpleTestCase):
    def test_each_part_is_its_share_of_the_bar_end_to_end(self):
        bar = AllocationBar("Example whole", (Share("Cash", 30.0, 1), Share("Held", 70.0, 3)))

        self.assertEqual(
            [(part["slot"], part["at"], part["width"]) for part in bar.drawn],
            [(1, 0.0, 30.0), (3, 30.0, 70.0)],
        )
        self.assertEqual(bar.spoken, "Example whole: Cash 30%, Held 70%")

    def test_a_sliver_still_shows_and_the_bar_still_fills(self):
        bar = AllocationBar("Example whole", (Share("Tiny", 0.01, 1), Share("Most", 999.99, 2)))

        tiny, most = bar.drawn
        self.assertEqual(tiny["width"], AllocationBar.FLOOR)
        self.assertAlmostEqual(tiny["width"] + most["width"], 100.0)
        self.assertEqual(most["at"], tiny["width"])

    def test_a_part_with_nothing_is_neither_drawn_nor_heard(self):
        bar = AllocationBar("Example whole", (Share("Cash", 5.0, 1), Share("None", 0.0, 2)))

        self.assertEqual([part["slot"] for part in bar.drawn], [1])
        self.assertNotIn("None", bar.spoken)
        self.assertEqual(AllocationBar("Example whole", (Share("None", 0.0, 2),)).drawn, ())

    def test_a_part_off_the_palette_or_not_an_amount_is_refused(self):
        for parts in ((Share("Cash", 1.0, 6),), (Share("Cash", -1.0, 1),), (Share("Cash", float("nan"), 1),)):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                AllocationBar("Example whole", parts)

    def test_the_partial_draws_each_part_with_no_style_attribute(self):
        bar = AllocationBar("Example whole", (Share("Cash", 1.0, 1), Share("Held", 3.0, 3)))

        html = render_to_string("partials/_allocation.html", {"allocation": bar})

        (drawing,) = select(html, "role", "img")
        self.assertEqual(drawing.attrs["aria-label"], "Example whole: Cash 25%, Held 75%")
        self.assertEqual([rect.attrs["width"] for rect in elements(html, "rect")], ["25.0%", "75.0%"])
        self.assertEqual(select(html, "style"), [])

    def test_the_legend_gives_each_part_its_swatch_amount_share_and_link(self):
        bar = AllocationBar(
            "Example whole",
            (
                Share("Cash", 30.0, 1, url="/example/?type=cash", detail="$30.00"),
                Share("Held", 0.0, 4, url="/example/?type=held", detail="$0.00"),
                Share("Owed", 70.0, 2, url="/example/?type=owed", detail="-$70.00", tone="attention"),
            ),
        )

        html = render_to_string("partials/_allocation.html", {"allocation": bar, "legend": True})

        self.assertEqual([href for _, href in links(html)], ["/example/?type=cash", "/example/?type=owed"])
        for (words, _), said in zip(links(html), (("$30.00", "Cash", "30%"), ("-$70.00", "Owed", "70%")), strict=True):
            for part in said:
                self.assertIn(part, words.split())
        swatches = [i.attrs["class"] for i in elements(html, "i")]
        self.assertEqual(swatches, [f"chart-series-{rect['slot']}" for rect in bar.drawn])
        (owed,) = [span for span in within(html, "data-allocation-legend") if "toned-attention" in span.classes]
        self.assertEqual(owed.words, "-$70.00")

    def test_a_legend_part_with_no_link_or_amount_still_reads_its_share(self):
        bar = AllocationBar("Example whole", (Share("Cash", 1.0, 1), Share("Held", 3.0, 3)))

        html = render_to_string("partials/_allocation.html", {"allocation": bar, "legend": True})

        self.assertEqual(links(html), [])
        rows = [
            row for row in within(html, "data-allocation-legend") if "data-allocation-legend" in row.ancestors[-1].attrs
        ]
        self.assertEqual(len(rows), 2)
        for row, said in zip(rows, (("Cash", "25%"), ("Held", "75%")), strict=True):
            for part in said:
                self.assertIn(part, row.words.split())

    def test_without_a_legend_the_partial_draws_only_the_bar(self):
        bar = AllocationBar("Example whole", (Share("Cash", 1.0, 1, url="/example/", detail="$1.00"),))

        html = render_to_string("partials/_allocation.html", {"allocation": bar})

        self.assertEqual(select(html, "data-allocation-legend"), [])
        self.assertEqual(links(html), [])

    def test_a_tone_outside_the_status_vocabulary_is_refused(self):
        with self.assertRaises(ValueError):
            Share("Owed", 1.0, 2, tone="warning")
        self.assertEqual(Share("Owed", 1.0, 2, tone="attention").tone, "attention")
