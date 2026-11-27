"""An allocation: parts of one whole drawn as one bar, with no width written by the page."""

from django.test import SimpleTestCase

from hq_sdk.ui import AllocationBar, Share


class AllocationTests(SimpleTestCase):
    def test_a_part_with_nothing_is_neither_drawn_nor_heard(self):
        bar = AllocationBar("Example whole", (Share("Cash", 5.0, 1), Share("None", 0.0, 2)))

        self.assertEqual([part["slot"] for part in bar.drawn], [1])
        self.assertNotIn("None", bar.spoken)
        self.assertEqual(AllocationBar("Example whole", (Share("None", 0.0, 2),)).drawn, ())

    def test_a_part_off_the_palette_or_not_an_amount_is_refused(self):
        for parts in ((Share("Cash", 1.0, 6),), (Share("Cash", -1.0, 1),), (Share("Cash", float("nan"), 1),)):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                AllocationBar("Example whole", parts)
