"""One reading of a comma-separated list."""

from django.test import SimpleTestCase


class SplitListTests(SimpleTestCase):
    def test_items_are_stripped_in_order(self):
        from ..lists import split_list

        self.assertEqual(split_list(" alpha, beta ,gamma "), ["alpha", "beta", "gamma"])

    def test_empty_and_blank_items_are_left_out(self):
        from ..lists import split_list

        self.assertEqual(split_list(",alpha,, ,beta,"), ["alpha", "beta"])

    def test_nothing_written_is_no_items(self):
        from ..lists import split_list

        self.assertEqual(split_list(""), [])
        self.assertEqual(split_list(" , "), [])

    def test_a_single_item_needs_no_comma(self):
        from ..lists import split_list

        self.assertEqual(split_list(" alpha "), ["alpha"])
