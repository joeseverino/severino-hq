"""What a capability call carries beside its command, read once for every door.

Read once the caller's authority is settled, so a bad target or stale-write
token is refused in one sentence wherever it was sent, and a caller without the
grant learns nothing of either.
"""

import re
from collections.abc import Mapping
from typing import Annotated, Any, NotRequired, TypedDict, cast

from pydantic import (
    ConfigDict,
    Field,
    Strict,
    StrictStr,
    TypeAdapter,
    ValidationError as PydanticValidationError,
    ValidationInfo,
    WithJsonSchema,
    with_config,
)

from .command_fields import Checked, Reason, RequiredText
from .integration_specs import TARGET_KINDS, CapabilitySpec
from .json_schemas import json_schema
from .moments import moment
from .reasons import declared_only
from .record_fields import RecordId
from .refusal_status import Refusal, refusal_of


def _as_its_kind(value: str | int, info: ValidationInfo) -> str | int:
    """``value``, refused where it is text its capability's target kind has a pattern for and does not match."""

    kind = TARGET_KINDS.get((info.context or {}).get("target_kind") or "")
    if kind is None or not kind.pattern or not isinstance(value, str) or re.fullmatch(kind.pattern, value):
        return value
    raise (_NOT_AN_ID if kind.coerce is int else _NOT_ITS_FORM).refused()


_NOT_AN_ID = Reason("target_id", "must be the id of the record it acts on")
_NOT_ITS_FORM = Reason("target_form", "must be in the form its schema's pattern gives")


def _whole(value: Any) -> Any:
    """``value`` as an integer where it is a JSON number with nothing after its point, which JSON Schema's
    ``integer`` admits (``2.0``); anything else unchanged, for the strict integer to judge."""

    return int(value) if isinstance(value, float) and value.is_integer() else value


def _moment(value: str) -> str:
    """``value`` unchanged once it reads as a date and time: it is compared with the record's ``updated_at`` as written."""

    if moment(value, zero_is_never=False) is None:
        raise ValueError
    return value


# The stale-write token every capability call may carry, one definition in
# every request schema.
type ExpectedUpdatedAt = Annotated[
    StrictStr,
    Checked(_moment, Reason("expected_updated_at", "must be a date and time, as the record's updated_at says it")),
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
            Annotated[RecordId, Strict(), Checked(_whole, mode="before")] | Annotated[RequiredText, Strict()],
            Checked(_as_its_kind),
            Field(description="The record the command acts on, by its identifier."),
        ]
    ]
    expected_updated_at: NotRequired[ExpectedUpdatedAt]


ENVELOPE = TypeAdapter(CallEnvelope)
declared_only(ENVELOPE.core_schema, "A call")
ENVELOPE_SCHEMA: Mapping[str, Any] = json_schema(ENVELOPE)


def _sent(value: Any) -> Any:
    """``value``, refused where it is JSON's null: a field is left out, never sent as nothing."""

    if value is None:
        raise _SENT.refused()
    return value


_SENT = Reason("null_sent", "must be left out of the body rather than sent as null")


@with_config(ConfigDict(extra="forbid"))
class _Body(TypedDict):
    """A request body's own form: its fields, a command that is an object, and nothing sent as null."""

    payload: NotRequired[dict[str, Any]]
    target: NotRequired[Annotated[Any, Checked(_sent)]]
    expected_updated_at: NotRequired[Annotated[Any, Checked(_sent)]]


_BODY = TypeAdapter(_Body)
declared_only(_BODY.core_schema, "A call's body")


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
