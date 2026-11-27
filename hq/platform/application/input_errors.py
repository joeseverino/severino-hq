"""The one answer an ``invalid_input`` refusal carries: its message and details.

Every validation failure an adapter answers with (Pydantic, a model's
``full_clean``, an unknown field) is told the same way (docs/API.md#errors).
Neither part carries a submitted value: a payload can hold a secret, and an
error travels further than the request did (logs, the audit trail, idempotency
records, agent transcripts) while the caller already has its own input.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, get_args

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError as DjangoValidationError
from pydantic_core import PydanticCustomError
from pydantic_core.core_schema import ErrorType

from hq.platform.core.rules import RULE

from .reasons import declared_words
from .refusal_codes import Code
from .ui import ended

# How many problems the sentence names before pointing at ``details``.
_NAMED = 5

_PYDANTIC_TYPES = frozenset(get_args(ErrorType))
_CHOICE = frozenset({"enum", "literal_error"})
_ERROR_CLAUSE = re.compile(r"[,:] ")
# Pydantic names its subject ("Input should be", "value is not"), which follows the field here.
_SHOULD = re.compile(r"^\w+ should ")
_IS = re.compile(r"^\w+ is ")
_EXPECTED_SPLIT = re.compile(r",\s*|\s+or\s+")

_DJANGO_REASONS = {
    "blank": "is required",
    "null": "is required",
    "required": "is required",
    "invalid_choice": "is not one of the allowed choices",
    "unique": "is already taken",
    "unique_together": "is already taken",
    Code.NOT_FOUND: "names nothing HQ has",
}


@dataclass(frozen=True, slots=True)
class InputRefusal:
    """What an ``invalid_input`` error says: one sentence and its evidence."""

    message: str
    details: Any


def pydantic_refusal(subject: str, errors: Iterable[Mapping[str, Any]]) -> InputRefusal:
    """A refusal from Pydantic-shaped errors, stripped of what the caller sent."""

    # Pydantic's ``input``, ``url`` and ``ctx`` are dropped: ``ctx`` can hold
    # the exception a validator raised, text and all.
    sent = list(map(_unknown_as_extra, errors))
    details = [
        {
            "type": error.get("type", ""),
            "loc": list(error.get("loc", ())),
            "msg": _said(error),
        }
        for error in sent
    ]
    problems = [
        (_path(error["loc"]), _pydantic_reason(error, original.get("ctx") or {}, original.get("input")))
        for error, original in zip(details, sent, strict=True)
    ]
    return InputRefusal(_message(subject, problems), details)


def django_refusal(subject: str, exc: DjangoValidationError) -> InputRefusal:
    """A refusal from a Django error, described by field and code, never text,
    since a model's messages interpolate values freely."""

    # Django gives an error built from a dict an error_dict, and any other none.
    grouped = exc.error_dict.items() if hasattr(exc, "error_dict") else [(NON_FIELD_ERRORS, exc.error_list)]
    details: dict[str, list[str]] = {}
    problems = []
    for field, errors in grouped:
        for error in errors:
            details.setdefault(field, []).append(error.code or "invalid")
            where = "" if field == NON_FIELD_ERRORS else field
            problems.append((where, _django_reason(error)))
    return InputRefusal(_message(subject, problems), details)


_EXTRA = {"type": "extra_forbidden", "msg": "Extra inputs are not permitted"}


def unknown_field_errors(fields: Sequence[str]) -> list[dict[str, Any]]:
    """Pydantic's ``extra_forbidden`` shape, for a field no Pydantic type refused."""

    return [{**_EXTRA, "loc": [field]} for field in fields]


def misnamed(name: str) -> PydanticCustomError:
    """The error of a field sent under a name its input does not take, naming
    the field ``name`` that would take it in its place."""

    return PydanticCustomError("misnamed", "is not a known field; send it as {name}", {"name": name})


def _unknown_as_extra(error: Mapping[str, Any]) -> Mapping[str, Any]:
    # A dataclass refuses a field it does not have as an unexpected keyword
    # argument, a model or TypedDict as an extra input: one refusal, told one way.
    return {**error, **_EXTRA} if error.get("type") == "unexpected_keyword_argument" else error


def _message(subject: str, problems: list[tuple[str, str]]) -> str:
    """``subject: field reason; field reason``: the one sentence for all of them."""

    # A problem with no field is about the input as a whole.
    named = [f"{where or 'the input'} {reason}" for where, reason in problems]
    if not named:
        return f"{subject}: the input is invalid."
    shown = "; ".join(named[:_NAMED])
    if len(named) > _NAMED:
        shown += f"; and {len(named) - _NAMED} more (see details)"
    return ended(f"{subject}: {shown}")


def _path(loc: Sequence[Any]) -> str:
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path


def _said(error: Mapping[str, Any]) -> str:
    """What an error says: a declared reason's words, or Pydantic's own."""

    kind = error.get("type", "")
    msg = str(error.get("msg", ""))
    declared = declared_words(kind)
    if kind not in _PYDANTIC_TYPES and declared is not None:
        return declared
    if kind in _CHOICE:
        # Only the declared choices, which may themselves hold ", ".
        return msg
    if kind == "union_tag_invalid":
        # The tag found is the caller's; the tags expected are the schema's.
        context = error.get("ctx") or {}
        return f"Input should have {context.get('discriminator', '')} one of {context.get('expected_tags', '')}"
    # Cut before the clause where a parser names what it read.
    return _ERROR_CLAUSE.split(msg, maxsplit=1)[0]


def _pydantic_reason(error: Mapping[str, Any], context: Mapping[str, Any], sent: object = None) -> str:
    kind = error["type"]
    if kind == "missing":
        return "is required"
    if kind == "extra_forbidden":
        return "is not a known field"
    if kind == "string_pattern_mismatch":
        # The pattern is for a machine; its details and the schema carry it.
        return "is not in the form its schema's pattern gives"
    msg = error["msg"]
    if kind in _CHOICE and msg.startswith("Input should be "):
        # Pydantic phrases the declared choices as "'a', 'b' or 'c'".
        choices = [
            choice.strip().strip("'\"")
            for choice in _EXPECTED_SPLIT.split(msg.removeprefix("Input should be "))
            if choice.strip()
        ]
        # The choice nearest what was sent names the retry, a declared value
        # and never the one sent.
        nearest = get_close_matches(sent, choices, n=1) if isinstance(sent, str) else []
        return f"must be one of {', '.join(choices)}" + (f" (nearest: {nearest[0]})" if nearest else "")
    # A bound, a number the schema declares, reads as the model's refusal of
    # it does, whichever of the two answers first.
    if kind == "less_than_equal" and "le" in context:
        return f"must be at most {context['le']}"
    if kind == "greater_than_equal" and "ge" in context:
        return f"must be at least {context['ge']}"
    if kind == "json_invalid":
        return "is not valid JSON"
    if kind == "string_type" and isinstance(sent, list):
        # Several values of a text field are one text, comma-separated.
        return "must be one text; send several values comma-separated"
    return _IS.sub("is ", _SHOULD.sub("must ", msg)) if msg else "is invalid"


def _django_reason(error: DjangoValidationError) -> str:
    code = error.code or ""
    if code == RULE:
        return f"is refused: {str(error.message).removesuffix('.')}"
    if code in _DJANGO_REASONS:
        return _DJANGO_REASONS[code]
    limit = (error.params or {}).get("limit_value")
    if code == "max_length" and isinstance(limit, int):
        return f"must have at most {limit} characters"
    if code == "min_length" and isinstance(limit, int):
        return f"must have at least {limit} characters"
    if code == "max_value" and isinstance(limit, int):
        return f"must be at most {limit}"
    if code == "min_value" and isinstance(limit, int):
        return f"must be at least {limit}"
    return "is invalid"
