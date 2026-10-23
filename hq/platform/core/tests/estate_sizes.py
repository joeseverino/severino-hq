"""Measuring one cost over two sizes, inside one test.

A cost that does not grow with rows is proven only where the larger draw holds
more rows: a cache or a stale graph that serves the smaller answer twice
measures the same query count at every size, and a comparison of counts alone
passes on it.
"""

import re
from collections.abc import Callable, Sequence

from django.db import connection, transaction
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.application.derivations import uncached
from hq.platform.core.bench import Seeded, seed

# Two sizes of the same estate: a cost that grows with rows differs between them.
SMALL, LARGE = 0.05, 0.25


def at_both_sizes[T](measure: Callable[[Seeded], T]) -> tuple[T, T]:
    """``measure`` over the small estate, then over the large one in its place."""

    with transaction.atomic():
        small = measure(seed(SMALL))
        transaction.set_rollback(True)
    return small, measure(seed(LARGE))


def _drawn[T](draw: Callable[[], T]) -> tuple[T, int]:
    # Every derivation runs inside the count, so it is the draw's own cost
    # rather than what the derived cache still holds.
    with uncached(), CaptureQueriesContext(connection) as queries:
        produced = draw()
    return produced, len(queries)


def assert_cost_does_not_grow[T](
    case: SimpleTestCase,
    *,
    sizes: Sequence[int],
    draw: Callable[[int], T],
    rows: Callable[[T], int],
    arrange: Callable[[int], object] = lambda _size: None,
    slack: int = 0,
) -> None:
    """``draw`` costs no more queries at the last size than at the first.

    For each size, ``arrange(size)`` lays out that many rows outside the count,
    then ``draw(size)`` is counted and ``rows`` of what it produced must be the
    size, so a draw served from an earlier one fails here. The first size is
    drawn once uncounted, so what a process loads on its first page does not
    pad the smaller count. ``slack`` is the queries the largest draw may add
    over the smallest.
    """

    def held(produced: T, size: int) -> None:
        case.assertEqual(rows(produced), size, f"the draw for {size} rows did not hold {size}")

    _assert_flat(case, sizes, draw, arrange, held, slack)


def assert_subject_cost_does_not_grow(
    case: SimpleTestCase,
    *,
    sizes: Sequence[int],
    draw: Callable[[int], object],
    arrange: Callable[[int], object],
) -> None:
    """``draw``, about one subject, costs no more queries in a larger estate.

    Only for a draw whose answer names its own subject and none of the rows
    ``arrange`` lays out, so nothing in it can count them; each draw runs
    uncached, so the larger count is still of the larger estate.
    """

    _assert_flat(case, sizes, draw, arrange, lambda _produced, _size: None, 0)


def _assert_flat[T](
    case: SimpleTestCase,
    sizes: Sequence[int],
    draw: Callable[[int], T],
    arrange: Callable[[int], object],
    held: Callable[[T, int], None],
    slack: int,
) -> None:
    costs: list[int] = []
    for size in sizes:
        arrange(size)
        if not costs:
            _drawn(lambda size=size: draw(size))
        produced, cost = _drawn(lambda size=size: draw(size))
        held(produced, size)
        costs.append(cost)
    case.assertLessEqual(costs[-1], costs[0] + slack, f"the cost grows with the rows: {costs} for {list(sizes)}")


def assert_estate_cost_does_not_grow[T](
    case: SimpleTestCase, *, draw: Callable[[Seeded], T], rows: Callable[[T], int]
) -> None:
    """``draw`` costs the same over the large bench estate as over the small.

    The bench estate's sizes are fractions, so the rows it holds are not known
    ahead: the large draw must hold more of them than the small.
    """

    small, large = at_both_sizes(lambda seeded: _drawn(lambda: draw(seeded)))
    (small_drawn, small_cost), (large_drawn, large_cost) = small, large
    case.assertGreater(rows(large_drawn), rows(small_drawn), "the large estate drew no more rows than the small")
    case.assertEqual(large_cost, small_cost, "the cost grows with the estate")


def distinct(pattern: str) -> Callable[[object], int]:
    """How many different matches of ``pattern`` a response's body or a text holds."""

    def count(produced: object) -> int:
        content = getattr(produced, "content", produced)
        text = content.decode() if isinstance(content, bytes) else str(content)
        return len(set(re.findall(pattern, text)))

    return count
