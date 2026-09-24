"""The one answer an ``invalid_input`` refusal carries: its message and details.

Every validation failure an adapter answers with -- a Pydantic command or query,
a Django model's ``full_clean``, a field the command does not have -- is told
to the caller the same way: which field, and why, in ``error.message``, with the
structured list in ``details``. The message is for a caller (often an agent)
that reads only the sentence.

Neither carries a submitted value. A payload can hold a secret, and an error
travels further than the request did -- into logs, the audit trail, idempotency
records and agent transcripts -- while the caller already has its own input. So
both are built from field names, error types, expected types and declared
choices only:

- Pydantic's ``input``, ``url`` and ``ctx`` are dropped (``ctx`` can hold the
  exception a validator raised, text and all).
- Pydantic's ``msg`` is kept only for built-in error types, cut before the
  clause where a few of them quote the input, and never for the types whose
  text is the caller's or a validator's own.
- Django's text is never used, because a model's messages interpolate values
  freely. Its details are field names and error codes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, get_args

from django.core.exceptions import NON_FIELD_ERRORS
from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic_core.core_schema import ErrorType

# How many problems the sentence names before pointing at ``details``.
_NAMED = 5

_PYDANTIC_TYPES = frozenset(get_args(ErrorType))
# Built-in types whose ``msg`` can carry caller text: a validator's own words,
# the offending tag, or a parser's complaint about a character.
_OPAQUE = frozenset(
    {
        "assertion_error",
        "get_attribute_error",
        "json_invalid",
        "union_tag_invalid",
        "value_error",
    }
)
_CHOICE = frozenset({"enum", "literal_error"})
_ERROR_CLAUSE = re.compile(r"[,:] ")
_SHOULD = re.compile(r"^\w+ should ")
_EXPECTED_SPLIT = re.compile(r",\s*|\s+or\s+")
_OPAQUE_MSG = "Input is invalid"

_DJANGO_REASONS = {
    "blank": "is required",
    "null": "is required",
    "required": "is required",
    "invalid_choice": "is not one of the allowed choices",
    "unique": "is already taken",
    "unique_together": "is already taken",
}


@dataclass(frozen=True)
class Refusal:
    """What an ``invalid_input`` error says: one sentence and its evidence."""

    message: str
    details: Any


def pydantic_refusal(subject: str, errors: Iterable[Mapping[str, Any]]) -> Refusal:
    """A refusal from Pydantic-shaped errors, stripped of what the caller sent."""

    details = [
        {
            "type": error.get("type", ""),
            "loc": list(error.get("loc", ())),
            "msg": _safe_msg(error) or _OPAQUE_MSG,
        }
        for error in errors
    ]
    problems = [(_path(error["loc"]), _pydantic_reason(error)) for error in details]
    return Refusal(_message(subject, problems), details)


def django_refusal(subject: str, exc: DjangoValidationError) -> Refusal:
    """A refusal from a Django error, described by field and code, never text."""

    if hasattr(exc, "error_dict"):
        grouped = exc.error_dict.items()
    else:
        grouped = [(NON_FIELD_ERRORS, exc.error_list)]
    details: dict[str, list[str]] = {}
    problems = []
    for field, errors in grouped:
        for error in errors:
            details.setdefault(field, []).append(error.code or "invalid")
            where = "" if field == NON_FIELD_ERRORS else field
            problems.append((where, _django_reason(error)))
    return Refusal(_message(subject, problems), details)


def unknown_field_errors(fields: Sequence[str]) -> list[dict[str, Any]]:
    """Pydantic's ``extra_forbidden`` shape, for a field a dataclass does not have."""

    return [
        {
            "type": "extra_forbidden",
            "loc": [field],
            "msg": "Extra inputs are not permitted",
        }
        for field in fields
    ]


def _message(subject: str, problems: list[tuple[str, str]]) -> str:
    """``subject: field reason; field reason`` -- the one sentence for all of them."""

    # A problem with no field is about the input as a whole.
    named = [f"{where or 'the input'} {reason}" for where, reason in problems]
    if not named:
        return f"{subject}: the input is invalid."
    shown = "; ".join(named[:_NAMED])
    if len(named) > _NAMED:
        shown += f"; and {len(named) - _NAMED} more (see details)"
    return f"{subject}: {shown}."


def _path(loc: Sequence[Any]) -> str:
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path


def _safe_msg(error: Mapping[str, Any]) -> str:
    """Pydantic's own text, when it cannot hold the input; otherwise empty."""

    kind = error.get("type", "")
    if kind not in _PYDANTIC_TYPES or kind in _OPAQUE:
        return ""
    if kind in _CHOICE:
        # Only the declared choices, which may themselves hold ", ".
        return str(error.get("msg", ""))
    return _ERROR_CLAUSE.split(str(error.get("msg", "")), maxsplit=1)[0]


def _pydantic_reason(error: Mapping[str, Any]) -> str:
    kind = error["type"]
    if kind == "missing":
        return "is required"
    if kind == "extra_forbidden":
        return "is not a known field"
    msg = error["msg"]
    if kind in _CHOICE and msg.startswith("Input should be "):
        # Pydantic phrases the declared choices as "'a', 'b' or 'c'".
        choices = [
            choice.strip().strip("'\"")
            for choice in _EXPECTED_SPLIT.split(msg.removeprefix("Input should be "))
            if choice.strip()
        ]
        return f"must be one of {', '.join(choices)}"
    if msg and msg != _OPAQUE_MSG:
        return _SHOULD.sub("must ", msg)
    return "is invalid"


def _django_reason(error: DjangoValidationError) -> str:
    code = error.code or ""
    if code in _DJANGO_REASONS:
        return _DJANGO_REASONS[code]
    limit = (error.params or {}).get("limit_value")
    if code == "max_length" and isinstance(limit, int):
        return f"must have at most {limit} characters"
    if code == "min_length" and isinstance(limit, int):
        return f"must have at least {limit} characters"
    return "is invalid"
