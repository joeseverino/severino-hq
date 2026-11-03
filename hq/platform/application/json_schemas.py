"""The one way HQ turns a Python type into the JSON Schema it publishes.

Every schema HQ hands a client (the API document, the capability and resource
catalogs, the MCP tools, the control plane's specs) comes from here, so all of
them say a thing the same way. A schema carries only the titles a declaration
gives, and a named alias is declared by its name: the title Pydantic would
derive from a field's name or a class's is only that name, and every reader
labels a field from its name through ``labels.field_label`` instead, while an
alias that names a bare composition (``type TotalCost = Annotated[Decimal,
...]``, a number or a string) is titled from its name by that same labeler, so
a viewer offering its branches says what it is rather than the name it is
filed under. A TypedDict's keys are all of its keys, as
the strict API reads them; an object open to any key says so by saying
nothing, JSON Schema's default.

A fragment many declarations share is a named type alias (``type Notes =
Annotated[str, Describe(...)]``): Pydantic writes it once, as the definition
named for the alias, and each use as a ``$ref`` to it beside what that use
adds (its ``default``). ``fields`` reads a schema's fields whole again.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any, override

from pydantic import GetCoreSchemaHandler, TypeAdapter
from pydantic.json_schema import DEFAULT_REF_TEMPLATE, GenerateJsonSchema, JsonSchemaMode, JsonSchemaValue
from pydantic_core import core_schema

from .labels import field_label

# Where a schema's own definitions are, as Pydantic refers to them.
LOCAL = "#/$defs/"


class PublishedSchema(GenerateJsonSchema):
    """Pydantic's generator, saying only what the declarations say."""

    # Whether a TypedDict that says nothing of other keys is closed: the API
    # reads every one strictly, so its document closes each.
    closes_typed_dicts = True

    @override
    def generate(self, schema: core_schema.CoreSchema, mode: JsonSchemaMode = "validation") -> JsonSchemaValue:
        value = super().generate(schema, mode)
        _title_compositions(value.get("$defs", {}))
        return value

    @override
    def generate_definitions(self, inputs: Any) -> tuple[dict[Any, JsonSchemaValue], dict[Any, JsonSchemaValue]]:
        refs, definitions = super().generate_definitions(inputs)
        _title_compositions(definitions)
        return refs, definitions

    @override
    def field_title_should_be_set(self, schema: Any) -> bool:
        # A declared ``Field(title=...)`` is in the field's schema already, and
        # Pydantic asks this only when it is not.
        return False

    @override
    def typed_dict_schema(self, schema: core_schema.TypedDictSchema) -> JsonSchemaValue:
        value = super().typed_dict_schema(schema)
        if self.closes_typed_dicts:
            value.setdefault("additionalProperties", False)
        return _unsaid(_untitled(value, schema.get("cls")))

    @override
    def model_schema(self, schema: core_schema.ModelSchema) -> JsonSchemaValue:
        return _untitled(super().model_schema(schema), schema["cls"])

    @override
    def dataclass_schema(self, schema: core_schema.DataclassSchema) -> JsonSchemaValue:
        return _untitled(super().dataclass_schema(schema), schema["cls"])

    @override
    def enum_schema(self, schema: core_schema.EnumSchema) -> JsonSchemaValue:
        return _untitled(super().enum_schema(schema), schema["cls"])

    @override
    def dict_schema(self, schema: core_schema.DictSchema) -> JsonSchemaValue:
        return _unsaid(super().dict_schema(schema))


class AnswerSchema(PublishedSchema):
    """The same, a TypedDict closed only where its declaration forbids other
    keys: an answer a client validates as it comes, which may gain a field."""

    closes_typed_dicts = False

    @override
    def generate_inner(self, schema: Any) -> JsonSchemaValue:
        # A part ``Written`` types, which the answer's own fields stop at.
        if (schema.get("metadata") or {}).get(_WRITTEN):
            return {"type": "object"}
        return super().generate_inner(schema)


def _title_compositions(definitions: Mapping[str, JsonSchemaValue]) -> None:
    # A definition that is only a choice between shapes is an alias's: a class
    # always says its own shape. Its name is its declaration.
    for name, definition in definitions.items():
        if is_bare_composition(definition) and "title" not in definition:
            definition["title"] = field_label(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower())


def is_bare_composition(definition: Mapping[str, Any]) -> bool:
    """Whether ``definition`` says nothing of its shape but a choice between shapes."""

    return any(key in definition for key in ("anyOf", "oneOf")) and not any(
        key in definition for key in ("type", "properties", "enum", "const", "$ref", "allOf")
    )


def _untitled(value: JsonSchemaValue, cls: Any) -> JsonSchemaValue:
    # A class's name is what a definition is filed under, never its title.
    if cls is not None and value.get("title") == cls.__name__:
        del value["title"]
    return value


def _unsaid(value: JsonSchemaValue) -> JsonSchemaValue:
    # ``additionalProperties: true`` means what saying nothing does, and a
    # viewer drawing an example from the schema invents a key for it.
    if value.get("additionalProperties") in (True, {}):
        del value["additionalProperties"]
    return value


# The mark ``Written`` leaves on the schema of the part it types.
_WRITTEN = "hq_written"


class Written:
    """``Annotated[dict[str, Any], Written(Kind)]``: a part the API document
    types whole, as ``Kind``, and an MCP tool's shape leaves an object. It is a
    dataclass as ``asdict`` writes it: a dict to the type checker and ``Kind``
    to every validator."""

    def __init__(self, kind: type) -> None:
        self.kind = kind

    def __get_pydantic_core_schema__(self, _source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        schema = handler(self.kind)
        schema["metadata"] = {**schema.get("metadata", {}), _WRITTEN: True}
        return schema


def json_schema(
    kind: Any,
    *,
    mode: JsonSchemaMode = "validation",
    ref_template: str = DEFAULT_REF_TEMPLATE,
    generator: type[PublishedSchema] = PublishedSchema,
) -> dict[str, Any]:
    """``kind``'s JSON Schema, a type or the ``TypeAdapter`` already built for one."""

    adapter = kind if isinstance(kind, TypeAdapter) else TypeAdapter(kind)
    return adapter.json_schema(mode=mode, ref_template=ref_template, schema_generator=generator)


def json_schemas(
    kinds: Mapping[str, Any], *, mode: JsonSchemaMode, ref_template: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Each of ``kinds``' schemas by its key, and the definitions they share by name."""

    adapters = [(key, mode, TypeAdapter(kind)) for key, kind in kinds.items()]
    refs, definitions = TypeAdapter.json_schemas(adapters, ref_template=ref_template, schema_generator=PublishedSchema)
    return {key: refs[(key, mode)] for key in kinds}, definitions.get("$defs", {})


# Where a value of a fixed set keeps the words a person reads for it, the key
# OpenAPI viewers (Scalar among them) draw beside each value of an ``enum``.
LABELS = "x-enumDescriptions"


def choices(options: Iterable[tuple[Any, str]]) -> dict[str, Any]:
    """A value that is one of ``options``' values, each with its label: an
    ``enum``, which every validator and viewer reads as a set of values, and
    the labels by value beside it, keyed as JSON keys an object."""

    pairs = tuple(options)
    return {"enum": [value for value, _label in pairs], LABELS: {str(value): label for value, label in pairs}}


def labeled_choices(schema: Mapping[str, Any]) -> list[tuple[Any, str]] | None:
    """Each value ``schema`` allows with its label, its own name where
    ``choices`` gave none, or None for a value not limited to a set."""

    values = schema.get("enum")
    if not values:
        return None
    labels = schema.get(LABELS, {})
    return [(value, labels.get(str(value), str(value))) for value in values]


def fields(schema: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each property of ``schema`` whole: one that names a definition is that
    definition, with what the property says beside its ``$ref`` over it."""

    definitions = schema.get("$defs", {})
    whole: dict[str, dict[str, Any]] = {}
    for name, field in schema.get("properties", {}).items():
        ref = field.get("$ref", "")
        said = {key: value for key, value in field.items() if key != "$ref"}
        whole[name] = {**definitions[ref.removeprefix(LOCAL)], **said} if ref.startswith(LOCAL) else field
    return whole
