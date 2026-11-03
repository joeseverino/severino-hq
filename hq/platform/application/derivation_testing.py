"""Holding the clock every derivation reads, or its cache, for a test."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import Token
from datetime import datetime

from django.utils import timezone

from . import derivations
from .derivations import _UNCACHED


@contextmanager
def frozen(moment: datetime | None = None) -> Iterator[datetime]:
    """The clock held at ``moment`` (now, when not given) for the block.

    An answer stored inside the block stands until a table it reads is written,
    never until a moment passes, so asking twice and being served the second
    time holds across midnight and on a slow machine alike.
    """

    at = moment or timezone.now()
    held = derivations.clock
    derivations.clock = lambda: at
    try:
        yield at
    finally:
        derivations.clock = held


def uncached() -> Token[bool]:
    """Derive inside ``with uncached():``, reading and storing nothing.

    For measuring what a derivation itself costs: a budget on its queries, a
    profile of its work.
    """

    return _UNCACHED.set(True)
