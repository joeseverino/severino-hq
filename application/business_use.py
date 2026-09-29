"""The share of a purchase used for the business: one rule, one owner.

A business-use percentage is a whole number from 0 to 100. The save services
for assets and expenses enforce it, so every adapter (web, CLI, HTTP API, MCP)
is held to it; the web forms' ``BusinessUseMixin`` calls the same check so a
person sees the refusal beside the field rather than after submitting.

The models still clamp on save. That clamp is a last line of defence for a row
written outside the services, not the rule: before the services enforced the
range, the API accepted 150 and stored 100 where the browser refused it.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

FIELD = "business_use_percentage"
MINIMUM = 0
MAXIMUM = 100


def check_business_use(value: int) -> int:
    """``value`` when it is a percentage from 0 to 100; otherwise refuse it.

    The error carries Django's own ``min_value``/``max_value`` code and limit,
    so a form shows it on the field and an adapter names the bound without
    repeating what was sent.
    """

    if value < MINIMUM:
        raise ValidationError(
            f"Must be between {MINIMUM} and {MAXIMUM}.",
            code="min_value",
            params={"limit_value": MINIMUM},
        )
    if value > MAXIMUM:
        raise ValidationError(
            f"Must be between {MINIMUM} and {MAXIMUM}.",
            code="max_value",
            params={"limit_value": MAXIMUM},
        )
    return value


def require_business_use(values: dict[str, Any]) -> None:
    """Refuse a command whose business-use percentage is out of range.

    Raised against the field, so ``full_clean``-shaped handling in every adapter
    names ``business_use_percentage`` as the problem.
    """

    try:
        check_business_use(values[FIELD])
    except ValidationError as exc:
        raise ValidationError({FIELD: exc.error_list}) from None
