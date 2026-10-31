"""What a command's schema says about its page, read here on its own so the
page tests do not take the product's reading of it on trust."""

from typing import Any

# The fields a command carries for its own bookkeeping.
BOOKKEEPING = frozenset({"idempotency_key", "reason"})
# The words a switch declares for its two values.
SWITCH_WORDS = ("on", "off")


def changed_fields(schema: dict[str, Any]) -> set[str]:
    return set(schema.get("properties", {})) - BOOKKEEPING


def optional_fields(schema: dict[str, Any]) -> set[str]:
    return changed_fields(schema) - set(schema.get("required", ()))


def sets_yes_or_no(schema: dict[str, Any]) -> bool:
    """Whether the command's one field is a boolean."""

    names = changed_fields(schema)
    if len(names) != 1:
        return False
    field = schema["properties"][next(iter(names))]
    kinds = [field.get("type")] + [choice.get("type") for choice in field.get("anyOf", ())]
    return "boolean" in kinds and not {kind for kind in kinds if kind} - {"boolean", "null"}
