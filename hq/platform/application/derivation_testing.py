"""Holding the clock every derivation reads, for a test."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from django.utils import timezone

from . import derivations


@contextmanager
def frozen(moment: datetime | None = None) -> Iterator[datetime]:
    """The clock held at ``moment`` (now, when not given) for the block.

    An answer stored inside the block stands until a table it reads is written,
    never until a moment passes, so asking twice and being served the second
    time holds across midnight and on a slow machine alike.
    """

    at = moment or timezone.now()
    held = derivations._clock
    derivations._clock = lambda: at
    try:
        yield at
    finally:
        derivations._clock = held
