"""Leaf contracts shared by integration emitters and the graph compiler."""

import inspect
from annotationlib import Format
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import BaseModel, TypeAdapter

from .search_contracts import SearchDefinition
from .security import Capability
from .ui import ended
lazy from . import records
lazy from .labels import human_label


@dataclass(frozen=True, slots=True)
class TargetKind:
    """How a declared target binds to and is coerced for a capability."""

    keyword: str
    coerce: Callable[[Any], Any]


# One declaration drives spec validation, handler binding and request coercion.
TARGET_KINDS: dict[str, TargetKind] = {
    "slug": TargetKind("current_slug", str),
    "doc_id": TargetKind("current_doc_id", str),
    "integer": TargetKind("current_id", int),
    "key": TargetKind("current_key", str),
}


@cache
def command_schema(command_type: type) -> dict[str, Any]:
    """Build an immutable command type's closed JSON Schema once."""

    schema = TypeAdapter(command_type).json_schema()
    schema.setdefault("additionalProperties", False)
    return schema


# The one retry field every capability that changes or queues something takes.
IDEMPOTENCY_FIELD = "idempotency_key"
_IDEMPOTENCY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "title": "Idempotency Key",
    "description": (
        "Optional. A repeat carrying the same key returns the first result instead of acting a second time."
    ),
    "minLength": 1,
    "maxLength": 128,
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
}


def declares_idempotency_key(command_type: type) -> bool:
    """Whether the command carries the key into its own domain operation."""

    return IDEMPOTENCY_FIELD in command_schema(command_type).get("properties", {})


# The caller's stale-write token, handed only to a handler that names it.
STALE_WRITE_KEYWORD = "expected_updated_at"


@cache
def takes_stale_write_token(handler: Callable[..., Any]) -> bool:
    """Whether a capability handler declares ``expected_updated_at`` by name.

    Only a handler that checks for a stale write declares it; every other
    handler is called without it.
    """

    parameter = inspect.signature(handler, annotation_format=Format.STRING).parameters.get(STALE_WRITE_KEYWORD)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def capability_schema(spec: CapabilitySpec) -> dict[str, Any]:
    """The input a capability accepts: its command's schema and the retry key.

    A capability whose effect is not ``read`` takes an optional
    ``idempotency_key``, whether or not its command type declares the field,
    and never requires it. A ``read`` takes none. A record command's fields
    say what its model allows (``records.as_its_model_declares``).
    """

    schema = records.as_its_model_declares(spec.command_type, command_schema(spec.command_type))
    if spec.effect == "read":
        return schema
    properties = dict(schema.get("properties", {}))
    properties[IDEMPOTENCY_FIELD] = dict(_IDEMPOTENCY_SCHEMA)
    derived = {**schema, "properties": properties}
    required = [name for name in schema.get("required", ()) if name != IDEMPOTENCY_FIELD]
    if required:
        derived["required"] = required
    else:
        derived.pop("required", None)
    return derived


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    name: str
    summary: str
    effect: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    command_type: type
    handler: Callable[..., dict[str, Any]]
    target_kind: str | None = None
    subject_resource: str | None = None
    target_label: str = ""
    target_help: str = ""
    target_query: tuple[tuple[str, str | int | float | bool], ...] = ()
    execution_notes: tuple[str, ...] = ()
    target_initial_fields: tuple[str, ...] = ()
    # What a person calls the command. Optional so an extension that has not
    # named its commands still composes; those read as their dotted name.
    label: str = ""
    # What its button says, where that is shorter than its name ("Remove"
    # beside "Remove a record"). Every button, link and toast for the command
    # reads its words from here, never from text of its own.
    verb: str = ""
    # The words for a mode or state of the command, by name, where one
    # button cannot say them all: ``(("all", "Refresh all"),)``.
    variants: tuple[tuple[str, str], ...] = ()

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)

    @property
    def title(self) -> str:
        """The command's name wherever a person reads it."""

        return self.label or human_label(self.name)

    def words(self, variant: str = "") -> str:
        """What a button for the command says: its verb, else its name, or
        one of its declared variants. An undeclared variant is a KeyError."""

        if variant:
            return dict(self.variants)[variant]
        return self.verb or self.title

    def requested(self, subject: str = "", *, again: bool = False) -> str:
        """What a press says once the command is asked for, in its own words."""

        return requested(self.words(), subject, again=again)


def requested(words: str, subject: str = "", *, again: bool = False) -> str:
    """What a press of the button saying ``words`` says once its work is asked
    for: the one sentence every action's toast is."""

    about = f" for {subject}" if subject else ""
    return ended(f"{words} {'was already requested' if again else 'requested'}{about}")


# What every adapter reads beside a resource query as the keys to return, so
# no query model may declare it (integration_validation).
RESERVED_QUERY_FIELD = "fields"


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """One declaration of a readable domain and every operation it supports."""

    name: str
    label: str
    summary: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    # Handlers may come from an extension, so their result is checked at the
    # call (application.resources) rather than trusted from the annotation.
    list_handler: Callable[..., object] | None = None
    list_query_type: type[BaseModel] | None = None
    detail_handler: Callable[..., object] | None = None
    identifier: str | None = None
    identifier_type: type = str
    # RecordNotFound is answered as not found without being listed; these are
    # the other types a detail handler raises for a missing record, such as an
    # extension model's DoesNotExist.
    not_found_errors: tuple[type[Exception], ...] = ()
    search: SearchDefinition | None = None
    web_route: str = ""
    # The handlers also take ``principal=``: the answer depends on what the
    # caller may see, not only on whether it may read the resource.
    pass_principal: bool = False

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)
