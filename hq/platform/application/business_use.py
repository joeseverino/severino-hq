"""The share of a purchase used for the business: a whole percentage, 0 to 100.

Declared once, on the model: ``VALIDATORS`` on the field, so ``full_clean``
refuses an out-of-range value on every path (a ModelForm shows it beside the
field, a command names the field and bound), and ``in_range`` as a check
constraint, so a row written around both still cannot store one.
"""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F
from django.db.models.functions import Round

FIELD = "business_use_percentage"
MINIMUM = 0
MAXIMUM = 100
MESSAGE = f"Must be between {MINIMUM} and {MAXIMUM}."

VALIDATORS = [
    MinValueValidator(MINIMUM, message=MESSAGE),
    MaxValueValidator(MAXIMUM, message=MESSAGE),
]


def deductible(help_text: str) -> models.GeneratedField:
    """``total_cost`` times the share, in cents, rounded as ``quantize_money``
    rounds, stored by the table so every write path (``update``,
    ``bulk_create``) keeps it current.

    SQLite holds a decimal as a double, in which 0.015 lies just under half a
    cent. Whole cents times a whole percentage is an exact integer, so the half
    is decided on an exact value and ``ROUND`` takes it away from zero."""

    cents = Round(F("total_cost") * 100, output_field=models.FloatField())
    hundredths_of_a_cent = cents * F(FIELD)
    return models.GeneratedField(
        expression=Round(hundredths_of_a_cent / 100.0) / 100.0,
        output_field=models.DecimalField(max_digits=12, decimal_places=2),
        db_persist=True,
        help_text=help_text,
    )


def in_range(name: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(**{f"{FIELD}__gte": MINIMUM, f"{FIELD}__lte": MAXIMUM}),
        name=name,
        violation_error_message=MESSAGE,
    )
