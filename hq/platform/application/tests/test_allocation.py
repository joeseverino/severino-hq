"""An allocation: parts of one whole drawn as one bar, with no width written by the page."""

from django.test import SimpleTestCase

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
