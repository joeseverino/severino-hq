"""One rounding rule for every amount HQ stores.

Rounding is not a formatting preference; it is arithmetic that has to be
identical everywhere or two surfaces holding the same money disagree by a cent
and neither is obviously wrong. Python's default is banker's rounding
(``ROUND_HALF_EVEN``); HQ rounds half-up.

So the rule lives here rather than in any one domain's models, where another
domain could reach it only by importing those models and an extension (which
imports ``hq_sdk`` and nothing else) could not reach it at all. A caller that
re-derives the quantize with ``Decimal("0.01")`` gets the other rounding mode.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENTS = Decimal("0.01")


def quantize_money(value: Decimal) -> Decimal:
    """Round to cents, half away from zero."""
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def to_money(value, default: Decimal | None = None) -> Decimal | None:
    """Coerce an untrusted number to a quantized ``Decimal``, or ``default``.

    For values arriving as JSON, where a float has already lost precision and
    ``Decimal(float)`` would preserve the loss exactly. Going through ``str``
    first is what makes ``1.1`` mean 1.1.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        return quantize_money(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return default


# A true minus sign. A hyphen beside a figure reads as a dash.
MINUS = "\u2212"


def money(value: Decimal | int | float | None, *, cents: bool = True) -> str:
    """An amount as every page writes one: "$1,234.50", and "\u2212$5.00" when negative.

    ``cents=False`` is for a figure that is scanned, never reconciled: a
    balance, a total over a year. Nothing shows the mark for a missing value.
    """

    from .ui import MISSING

    amount = to_money(value)
    if amount is None:
        return MISSING
    sign = MINUS if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}" if cents else f"{sign}${abs(amount):,.0f}"


__all__ = ["CENTS", "MINUS", "money", "quantize_money", "to_money"]
