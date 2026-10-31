"""One rounding rule for every amount HQ stores.

Rounding is arithmetic that has to be identical everywhere, or two surfaces
holding the same money disagree by a cent and neither is obviously wrong. It
lives here rather than in one domain's models, so every domain reaches it, and
an extension (which imports ``hq_sdk`` and nothing else) through the SDK.
"""

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import overload

lazy from .ui import MISSING

CENTS = Decimal("0.01")


def quantize_money(value: Decimal) -> Decimal:
    """Round to cents, half away from zero."""
    # Python's default is banker's rounding (``ROUND_HALF_EVEN``), so a caller
    # that re-derives the quantize with ``Decimal("0.01")`` gets the other mode.
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


@overload
def to_money(value: object, default: Decimal) -> Decimal:
    pass


@overload
def to_money(value: object, default: None = None) -> Decimal | None:
    pass


def to_money(value: object, default: Decimal | None = None) -> Decimal | None:
    """Coerce an untrusted number to a quantized ``Decimal``, or ``default``.

    For values arriving as JSON, where a float has already lost precision and
    ``Decimal(float)`` would preserve the loss exactly. Going through ``str``
    first is what makes ``1.1`` mean 1.1.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        return quantize_money(Decimal(str(value)))
    except InvalidOperation, ValueError, TypeError:
        return default


# A true minus sign. A hyphen beside a figure reads as a dash.
MINUS = "\u2212"

# UAX #14 gives a sign and the currency mark after it the same class, PR, and
# allows a break between two PRs, so "\u2212$5.00" can end a line after its
# sign. A WORD JOINER (class WJ, rule LB11: no break before or after it) holds
# the sign to the figure, and draws nothing.
WORD_JOINER = "\u2060"


def signed(figure: str, *, negative: bool, plus: bool = False) -> str:
    """``figure`` behind its sign, the two one unbreakable unit.

    ``plus`` writes "+" before a figure that is not negative, for a change
    rather than a balance.
    """

    sign = MINUS if negative else "+" if plus else ""
    return f"{sign}{WORD_JOINER}{figure}" if sign else figure


def signed_number[N: (Decimal, float, int)](value: N | None, figure: Callable[[N], str], *, plus: bool = False) -> str:
    """``value`` written by ``figure`` and signed by the rule every amount is.

    ``figure`` writes a magnitude in its own units ("1:15/mi", "29 bpm"), never
    a sign. ``plus`` writes "+" before a figure that is not negative, for a
    change. A value whose figure writes as zero's is not negative, so nothing
    reads "−0:00". Nothing shows the mark for a missing value.
    """

    if value is None:
        return MISSING
    written = figure(abs(value))
    return signed(written, negative=value < 0 and written != figure(value - value), plus=plus)


def money(value: Decimal | int | float | None, *, cents: bool = True, plus: bool = False) -> str:
    """An amount as every page writes one: "$1,234.50", and "\u2212$5.00" when negative.

    ``cents=False`` is for a figure that is scanned, never reconciled: a
    balance, a total over a year. ``plus`` signs a change that is not
    negative, "+$5.00". A debt is a negative value. Nothing shows the mark for a missing
    value.
    """

    places = 2 if cents else 0
    return signed_number(to_money(value), lambda amount: f"${amount:,.{places}f}", plus=plus)


__all__ = ["CENTS", "money", "quantize_money", "signed", "signed_number", "to_money"]
