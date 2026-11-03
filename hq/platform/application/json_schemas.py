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
from types import get_original_bases
from typing import Any, TypedDict, is_typeddict, override

from pydantic import GetCoreSchemaHandler, TypeAdapter
from pydantic.json_schema import DEFAULT_REF_TEMPLATE, GenerateJsonSchema, JsonRef, JsonSchemaMode, JsonSchemaValue
from pydantic_core import core_schema

from .labels import field_label

# Where a schema's own definitions are, as Pydantic refers to them.
LOCAL = "#/$defs/"


class PublishedSchema(GenerateJsonSchema):
    """Pydantic's generator, saying only what the declarations say."""

    # Whether a TypedDict that says nothing of other keys is closed: the API
    # reads every one strictly, so its document closes each.
    closes_typed_dicts = True
    # Whether a TypedDict that extends another refers to each field it
    # inherits, the base a definition of its own (``DocumentSchema``).
    refers_inherited = False

    @override
    def generate(self, schema: core_schema.CoreSchema, mode: JsonSchemaMode = "validation") -> JsonSchemaValue:
        value = super().generate(schema, mode)
        _title_compositions(value.get("$defs", {}))
        _point_inherited(value)
        return value

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
        if self.refers_inherited and (cls := schema.get("cls")) is not None:
            self._refer_inherited(value, cls)
        return _unsaid(_untitled(value, schema.get("cls")))

    def _refer_inherited(self, value: JsonSchemaValue, cls: type) -> None:
        # Each property the base already says becomes a reference to the
        # base's, so a field is said once, where it is declared. A subclass
        # adding no key is the base under another name, and is written whole.
        # A field a base inherits in turn is referred to where it is declared.
        properties = value.get("properties", {})
        for base in _ancestors(cls):
            if _keys(base) >= _keys(cls):
                continue
            reference = self.generate_inner(TypeAdapter(base).core_schema)
            said = (self.get_schema_from_definitions(JsonRef(reference.get("$ref", ""))) or {}).get("properties", {})
            for name, field in said.items():
                if properties.get(name) == field:
                    properties[name] = {"$ref": reference["$ref"], _INHERITED: name}

    @override
    def generate_definitions(self, inputs: Any) -> tuple[dict[Any, JsonSchemaValue], dict[Any, JsonSchemaValue]]:
        refs, definitions = super().generate_definitions(inputs)
        _title_compositions(definitions)
        _point_inherited(definitions)
        return refs, definitions

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


class DocumentSchema(PublishedSchema):
    """The same, a TypedDict that extends another saying each field it
    inherits as a reference to its base's: the API document, whose every
    definition is a component a JSON pointer can reach into. A base's fields
    are referred to one by one, never through ``allOf``, since a closed base
    refuses every key its extension adds."""

    refers_inherited = True


# The key a reference to an inherited field carries until Pydantic has named
# every definition, when it becomes a pointer to the base's property.
_INHERITED = "hq_inherited"


def _keys(typed_dict: Any) -> frozenset[str]:
    return typed_dict.__required_keys__ | typed_dict.__optional_keys__


def _ancestors(cls: type) -> list[type]:
    """Each TypedDict ``cls`` extends, its own bases first."""

    found: list[type] = []
    waiting = list(get_original_bases(cls))
    while waiting:
        base = waiting.pop(0)
        if is_typeddict(base) and base is not TypedDict and base not in found:
            found.append(base)
            waiting.extend(get_original_bases(base))
    return found


def _point_inherited(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _point_inherited(item)
    elif isinstance(value, dict):
        if isinstance(name := value.get(_INHERITED), str) and "$ref" in value:
            del value[_INHERITED]
            value["$ref"] += "/properties/" + name.replace("~", "~0").replace("/", "~1")
        for item in value.values():
            _point_inherited(item)


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
    refs, definitions = TypeAdapter.json_schemas(adapters, ref_template=ref_template, schema_generator=DocumentSchema)
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
