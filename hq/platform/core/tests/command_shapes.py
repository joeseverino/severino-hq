"""What a command's schema says about its page, read here on its own so the
page tests do not take the product's reading of it on trust."""

from typing import Any

# The fields a command carries for its own bookkeeping.
BOOKKEEPING = frozenset({"idempotency_key", "reason"})
# The words a switch declares for its two values.
SWITCH_WORDS = ("on", "off")


def field_schema(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """One property whole: a named alias's definition, with what the use says beside its ``$ref`` over it."""

    field = schema["properties"][name]
    ref = field.get("$ref", "")
    if not ref.startswith("#/$defs/"):
        return field
    return {
        **schema["$defs"][ref.removeprefix("#/$defs/")],
        **{key: value for key, value in field.items() if key != "$ref"},
    }


def changed_fields(schema: dict[str, Any]) -> set[str]:
    return set(schema.get("properties", {})) - BOOKKEEPING


def optional_fields(schema: dict[str, Any]) -> set[str]:
    return changed_fields(schema) - set(schema.get("required", ()))


def sets_yes_or_no(schema: dict[str, Any]) -> bool:
    """Whether the command's one field is a boolean."""

    names = changed_fields(schema)
    if len(names) != 1:
        return False
    field = field_schema(schema, next(iter(names)))
    kinds = [field.get("type")] + [choice.get("type") for choice in field.get("anyOf", ())]
    return "boolean" in kinds and not {kind for kind in kinds if kind} - {"boolean", "null"}
