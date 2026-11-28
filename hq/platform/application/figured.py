"""Words that say a number, carrying it.

A leaf, importing nothing, so each function that words a number (an amount,
a measure, a count, a length of time) returns it, and a table orders the
words by what they say, never by reading them back.
"""

from decimal import Decimal
from typing import Self

# The units a length is said in, for one and for many. ``counted`` refuses
# them, so ``moments.Span`` is the one place a length is counted.
UNITS = {
    "second": "seconds",
    "minute": "minutes",
    "hour": "hours",
    "day": "days",
    "week": "weeks",
    "month": "months",
    "year": "years",
}


class Figure(str):
    """Words saying a number ("$1,234.50", "4.25 mi", "3 jobs", "2 days"), with the number."""

    figure: Decimal | float | int

    def __new__(cls, words: str, figure: Decimal | float | int) -> Self:
        said = super().__new__(cls, words)
        said.figure = figure
        return said

    # A derived read is cached pickled, and str's own arguments would leave the number out.
    def __getnewargs__(self) -> tuple[str, Decimal | float | int]:  # type: ignore[override]  # str declares one argument
        return str(self), self.figure
