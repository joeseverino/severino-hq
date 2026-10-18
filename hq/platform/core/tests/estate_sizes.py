"""Measuring one cost over two sizes of the bench estate, inside one test."""

from collections.abc import Callable

from django.db import transaction

from hq.platform.core.bench import Seeded, seed

# Two sizes of the same estate: a cost that grows with rows differs between them.
SMALL, LARGE = 0.05, 0.25


def at_both_sizes[T](measure: Callable[[Seeded], T]) -> tuple[T, T]:
    """``measure`` over the small estate, then over the large one in its place."""

    with transaction.atomic():
        small = measure(seed(SMALL))
        transaction.set_rollback(True)
    return small, measure(seed(LARGE))
