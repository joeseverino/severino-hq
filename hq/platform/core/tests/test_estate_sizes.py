from django.db import connection
from django.test import TestCase

from hq.platform.application import derivations

from .estate_sizes import assert_cost_does_not_grow, distinct


def _rows(size: int) -> list[int]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return list(range(size))


class CostDoesNotGrowTests(TestCase):
    def test_a_flat_draw_of_every_row_passes(self):
        assert_cost_does_not_grow(self, sizes=(1, 6), draw=_rows, rows=len)

    def test_a_draw_that_holds_one_row_at_every_size_fails(self):
        with self.assertRaisesMessage(AssertionError, "the draw for 6 rows did not hold 6"):
            assert_cost_does_not_grow(self, sizes=(1, 6), draw=lambda _size: _rows(1), rows=len)

    def test_a_query_per_row_fails(self):
        def per_row(size):
            return [_rows(1)[0] for _ in range(size)]

        with self.assertRaisesMessage(AssertionError, "the cost grows with the rows"):
            assert_cost_does_not_grow(self, sizes=(1, 6), draw=per_row, rows=len)

    def test_the_rows_are_laid_out_outside_the_count_and_drawn_uncached(self):
        laid, seen = [], []

        def draw(size):
            seen.append(derivations._UNCACHED.get())
            return laid[-size:]

        def arrange(size):
            laid.extend(_rows(1)[0] for _ in range(size))

        assert_cost_does_not_grow(self, sizes=(1, 6), arrange=arrange, draw=draw, rows=len)
        self.assertEqual(seen, [True, True])

    def test_distinct_counts_different_names_in_a_body(self):
        self.assertEqual(distinct(r"app-\d+")(b"app-1 app-2 app-1 web-3"), 2)
