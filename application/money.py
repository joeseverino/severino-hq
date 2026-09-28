"""One rounding rule for every amount HQ stores.

Rounding is not a formatting preference; it is arithmetic that has to be
identical everywhere or two surfaces holding the same money disagree by a cent
and neither is obviously wrong. Python's default is banker's rounding
(``ROUND_HALF_EVEN``), which is a defensible choice and *not* the one this
codebase already made: assets and expenses have quantized half-up since they
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
from typing import Annotated

from django.core.exceptions import ValidationError
from pydantic import Field

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


# The share of a purchase used for the business, as a whole percentage. One
# range, published in the API schema through the type and enforced by every
# save through ``business_use``, so no interface accepts what another refuses.
BUSINESS_USE_MIN, BUSINESS_USE_MAX = 0, 100
BusinessUse = Annotated[int, Field(ge=BUSINESS_USE_MIN, le=BUSINESS_USE_MAX)]


def business_use(value) -> int:
    """A business-use percentage, or a ``ValidationError`` when out of range."""

    percentage = int(value or 0)
    if not BUSINESS_USE_MIN <= percentage <= BUSINESS_USE_MAX:
        raise ValidationError(
            f"Must be between {BUSINESS_USE_MIN} and {BUSINESS_USE_MAX}.",
            code="business_use_range",
        )
    return percentage


def business_use_field(value) -> int:
    """``business_use`` for a save: the error names the field it is about."""

    try:
        return business_use(value)
    except ValidationError as exc:
        raise ValidationError({"business_use_percentage": exc.error_list}) from None


__all__ = [
    "BusinessUse",
    "CENTS",
    "business_use",
    "business_use_field",
    "quantize_money",
    "to_money",
]
