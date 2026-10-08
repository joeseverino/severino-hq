"""Leaf contracts shared by integration emitters and the graph compiler."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import BaseModel, TypeAdapter

from .search_contracts import SearchDefinition
from .security import Capability


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
        "Optional. A repeat carrying the same key returns the first result "
        "instead of acting a second time."
    ),
    "minLength": 1,
    "maxLength": 128,
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
}


def declares_idempotency_key(command_type: type) -> bool:
    """Whether the command carries the key into its own domain operation."""

    return IDEMPOTENCY_FIELD in command_schema(command_type).get("properties", {})


def capability_schema(spec: CapabilitySpec) -> dict[str, Any]:
    """The input a capability accepts: its command's schema and the retry key.

    A capability whose effect is not ``read`` takes an optional
    ``idempotency_key``, whether or not its command type declares the field,
    and never requires it. A ``read`` takes none.
    """

    schema = command_schema(spec.command_type)
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


@dataclass(frozen=True)
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

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)

    @property
    def title(self) -> str:
        """The command's name wherever a person reads it."""

        from .labels import human_label

        return self.label or human_label(self.name)


@dataclass(frozen=True)
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
