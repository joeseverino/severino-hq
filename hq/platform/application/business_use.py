"""The share of a purchase used for the business: a whole percentage, 0 to 100.

Declared once, on the model: ``VALIDATORS`` on the field, so ``full_clean``
refuses an out-of-range value on every path (a ModelForm shows it beside the
field, a command names the field and bound), and ``in_range`` as a check
constraint, so a row written around both still cannot store one.
"""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

FIELD = "business_use_percentage"
MINIMUM = 0
MAXIMUM = 100
MESSAGE = f"Must be between {MINIMUM} and {MAXIMUM}."

VALIDATORS = [
    MinValueValidator(MINIMUM, message=MESSAGE),
    MaxValueValidator(MAXIMUM, message=MESSAGE),
]


def in_range(name: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(**{f"{FIELD}__gte": MINIMUM, f"{FIELD}__lte": MAXIMUM}),
        name=name,
        violation_error_message=MESSAGE,
    )
