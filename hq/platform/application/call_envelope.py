"""What a capability call carries beside its command, read once for every door.

``capabilities.run_capability`` reads every door's target and stale-write
token with it, once the caller's authority is settled, so a target that is not
of its capability's kind or a token that is no time is refused with one
sentence wherever it was sent, and a caller without the grant learns nothing
of either. The API reads a body's own form (``read_body``) before it asks. The
API document and the capability catalog publish its schema
(``api.views.request_schema``).
"""

import re
from collections.abc import Mapping
from typing import Annotated, Any, NotRequired, TypedDict, cast

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError as PydanticValidationError,
    ValidationInfo,
    WithJsonSchema,
    with_config,
)
from pydantic_core import PydanticKnownError

from .integration_specs import CapabilitySpec
from .json_schemas import json_schema
from .refusal_status import Refusal, refusal_of
from .timestamps import moment

# What an integer target may be sent as besides a number: its digits.
INTEGER_TARGET = r"^-?[0-9]+$"
_INTEGER_DIGITS = re.compile(INTEGER_TARGET)


def _as_its_kind(value: str | int, info: ValidationInfo) -> str | int:
    """``value``, refused where the capability's target is an integer and it is text of anything but digits."""

    kind = (info.context or {}).get("target_kind")
    if kind == "integer" and isinstance(value, str) and not _INTEGER_DIGITS.fullmatch(value):
        # Pydantic's own type, whose fixed words name the kind; a validator's own text is never shown.
        raise PydanticKnownError("int_parsing")
    return value


def _moment(value: str) -> str:
    """``value`` unchanged once it reads as a date and time: it is compared with the record's ``updated_at`` as written."""

    if moment(value, zero_is_never=False) is None:
        raise ValueError("not a date and time")
    return value


# The stale-write token every capability call may carry, one definition in
# every request schema.
type ExpectedUpdatedAt = Annotated[
    StrictStr,
    AfterValidator(_moment),
    # The whole schema: inside an alias, this replaces what a Field beside it would say.
    WithJsonSchema(
        {
            "type": "string",
            "format": "date-time",
            "description": (
                "The target's `updated_at`, copied as it was last read. Where the command checks it, a record "
                "changed since is refused `conflict`; without it the record is taken as it is now."
            ),
            "examples": ["2026-11-02T14:30:00.123456+00:00"],
        }
    ),
]


@with_config(ConfigDict(extra="forbid"))
class CallEnvelope(TypedDict):
    """A capability call: the command, the record it acts on, and that record as it was last read."""

    payload: NotRequired[
        Annotated[dict[str, Any], Field(description="The command: the fields its input schema names.")]
    ]
    target: NotRequired[
        Annotated[
            StrictInt | Annotated[StrictStr, Field(min_length=1)],
            AfterValidator(_as_its_kind),
            Field(description="The record the command acts on, by its identifier."),
        ]
    ]
    expected_updated_at: NotRequired[ExpectedUpdatedAt]


ENVELOPE = TypeAdapter(CallEnvelope)
ENVELOPE_SCHEMA: Mapping[str, Any] = json_schema(ENVELOPE)


def _sent(value: Any) -> Any:
    """``value``, refused where it is JSON's null: a field is left out, never sent as nothing."""

    if value is None:
        raise ValueError("a field without a value is left out of the body")
    return value


@with_config(ConfigDict(extra="forbid"))
class _Body(TypedDict):
    """A request body's own form: its fields, a command that is an object, and nothing sent as null."""

    payload: NotRequired[dict[str, Any]]
    target: NotRequired[Annotated[Any, AfterValidator(_sent)]]
    expected_updated_at: NotRequired[Annotated[Any, AfterValidator(_sent)]]


_BODY = TypeAdapter(_Body)


def read_body(name: str, body: Mapping[str, Any]) -> CallEnvelope | Refusal:
    """``body`` as the fields of a call of ``name``, or the refusal of its form.

    Judges no value its target or stamp holds, which ``read_call`` reads once
    the caller may run the capability.
    """

    try:
        return cast(CallEnvelope, _BODY.validate_python(body))
    except PydanticValidationError as exc:
        return refusal_of(exc, name=name)


def read_call(spec: CapabilitySpec, sent: Mapping[str, Any]) -> CallEnvelope | Refusal:
    """``sent`` as a call of ``spec``, or the refusal of what it carries."""

    try:
        return ENVELOPE.validate_python(sent, context={"target_kind": spec.target_kind})
    except PydanticValidationError as exc:
        return refusal_of(exc, name=spec.name)
