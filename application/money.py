"""One rounding rule for every amount HQ stores.

Rounding is not a formatting preference; it is arithmetic that has to be
identical everywhere or two surfaces holding the same money disagree by a cent
and neither is obviously wrong. Python's default is banker's rounding
(``ROUND_HALF_EVEN``), which is a defensible choice and *not* the one this
codebase already made -- assets and expenses have quantized half-up since they
were written.

So the rule lives here rather than in whichever domain needed it first. It was
in ``assets.models``, where a second domain could only reach it by importing
another domain's models, and an extension could not reach it at all: extensions
import ``hq_sdk`` and nothing else. Re-deriving a one-line quantize looks
harmless right up to the point where one caller writes ``Decimal("0.01")`` and
gets the other rounding mode.
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


__all__ = ["CENTS", "quantize_money", "to_money"]
