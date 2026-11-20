"""The process caches of what HQ derives from its composition and adopted catalog.

Each holds one answer, for the arguments it was last asked with, which are
what it is derived from, so nothing clears it when an input changes. The one
input an argument cannot carry is a test's patch of a function it calls, so
the test runner puts back what each held as a patch began once the patch ends
(``isolation_testing``).
"""

from collections.abc import Callable
from typing import Any


class Kept[T]:
    """``function``'s answer for the arguments it was last asked with."""

    def __init__(self, function: Callable[..., T]) -> None:
        self.function = function
        self.held: tuple[tuple[Any, ...], T] | None = None

    def __call__(self, *arguments: Any) -> T:
        held = self.held
        if held is not None and held[0] == arguments:
            return held[1]
        answer = self.function(*arguments)
        self.held = (arguments, answer)
        return answer

    def cache_clear(self) -> None:
        self.held = None


KEPT: list[Kept[Any]] = []


def kept[T](function: Callable[..., T]) -> Kept[T]:
    """``function`` held as a ``Kept``, listed in ``KEPT``."""

    cached = Kept(function)
    KEPT.append(cached)
    return cached
