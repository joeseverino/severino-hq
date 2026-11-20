"""The one way HQ turns a Python type into the JSON Schema it publishes, so every schema says a thing the same way.

A schema carries only the titles a declaration gives, and readers label a field
through ``labels.field_label``. A fragment many declarations share is a named
type alias, defined once; a bound goes in the alias, since Pydantic writes one
given at a use into the alias's definition.
"""

import copy
import functools
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from types import get_original_bases
from typing import Annotated, Any, TypedDict, is_typeddict, override

from pydantic import GetCoreSchemaHandler, PlainSerializer, TypeAdapter
from pydantic.json_schema import DEFAULT_REF_TEMPLATE, GenerateJsonSchema, JsonRef, JsonSchemaMode, JsonSchemaValue
from pydantic_core import core_schema

from .answer_fields import ObservedAt
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
        _standard_formats(value)
        _title_compositions(value.get("$defs", {}))
        _title_branches(value, value.get("$defs", {}))
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
        _standard_formats(definitions)
        _title_compositions(definitions)
        _title_branches([refs, definitions], definitions)
        _point_inherited(definitions)
        return refs, definitions

    @override
    def model_schema(self, schema: core_schema.ModelSchema) -> JsonSchemaValue:
        return _untitled(super().model_schema(schema), schema["cls"])

    @override
    def dataclass_schema(self, schema: core_schema.DataclassSchema) -> JsonSchemaValue:
        written = _untitled(super().dataclass_schema(schema), schema["cls"])
        if self.mode == "serialization":
            written["additionalProperties"] = False
        return written

    @override
    def field_is_required(
        self,
        field: core_schema.ModelField | core_schema.DataclassField | core_schema.TypedDictField,
        total: bool,
    ) -> bool:
        # A dataclass is written whole (``as_written``, ``asdict``): each answer
        # holds every field it publishes, one at its default too, and no other.
        if self.mode == "serialization" and field["type"] == "dataclass-field":
            return field.get("serialization_exclude_if") is None
        return super().field_is_required(field, total)

    @override
    def generate_inner(self, schema: Any) -> JsonSchemaValue:
        value = super().generate_inner(schema)
        # Pydantic titles an enum by its class after ``enum_schema`` answers,
        # in the definition the value may refer to.
        if isinstance(schema, dict) and schema.get("type") == "enum":
            _untitled(self.resolve_ref_schema(value), schema["cls"])
        return value

    @override
    def handle_ref_overrides(self, json_schema: JsonSchemaValue) -> JsonSchemaValue:
        kept = super().handle_ref_overrides(json_schema)
        # Pydantic drops a sibling the referred definition already says, but
        # labels are read beside the enum they label (``labeled_choices``).
        if LABELS in kept and "enum" in json_schema:
            kept["enum"] = json_schema["enum"]
        return kept

    @override
    def decimal_schema(self, schema: core_schema.DecimalSchema) -> JsonSchemaValue:
        value = super().decimal_schema(schema)
        digits, places = schema.get("max_digits"), schema.get("decimal_places")
        if digits is None or places is None or "anyOf" not in value:
            return value
        # Pydantic refuses past ``max_digits`` and ``decimal_places`` and says
        # neither: a number is held to the same size by its bounds and step.
        whole = 10 ** (digits - places)
        number = value["anyOf"][0]
        number.update({"exclusiveMinimum": -whole, "exclusiveMaximum": whole, "multipleOf": 10**-places})
        return value

    @override
    def get_decimal_pattern(self, schema: core_schema.DecimalSchema) -> str | None:
        # The digits Pydantic counts: leading zeros, and zeros ending the
        # fraction, are none of them; an exponent is left unsaid.
        digits, places = schema.get("max_digits"), schema.get("decimal_places")
        if digits is None or places is None:
            return None
        whole = f"[0-9]{{1,{digits - places}}}" if digits > places else "0"
        fraction = f"[0-9]{{1,{places}}}0*" if places else "0+"
        return rf"^[+-]?(?:0*{whole}(?:\.(?:{fraction})?)?|0*\.{fraction})$"

    @override
    def dict_schema(self, schema: core_schema.DictSchema) -> JsonSchemaValue:
        value = _unsaid(super().dict_schema(schema))
        # Pydantic refuses a key outside its pattern but writes the pattern as
        # patternProperties alone, which admits any other key.
        if "patternProperties" in value:
            value.setdefault("additionalProperties", False)
        return value


class SentSchema(PublishedSchema):
    """The same, for what a caller sends, which never builds with an integer
    that names no ceiling: one past the database's integer would reach it
    unrefused. A record's id is ``record_fields.RecordId``."""

    @override
    def generate(self, schema: core_schema.CoreSchema, mode: JsonSchemaMode = "validation") -> JsonSchemaValue:
        # Read once written whole, since an annotation may write its bound
        # after the integer it wraps.
        value = super().generate(schema, mode)
        if unbounded := next(_unbounded(value), None):
            raise TypeError(
                f"The integer at {unbounded} a caller sends declares no ceiling: give it one, "
                "Annotated[int, Field(le=...)], or RecordId for an id."
            )
        return value


# What holds an integer to a ceiling, beside its type or the reference to it.
_BOUNDS = frozenset({"maximum", "exclusiveMaximum", "enum", "const"})


def _unbounded(node: Any, where: str = "#") -> Iterator[str]:
    """Where under ``node`` an integer names no ceiling."""

    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from _unbounded(item, f"{where}/{index}")
        return
    if not isinstance(node, dict) or _BOUNDS & node.keys():
        return
    kinds = node.get("type")
    if kinds == "integer" or (isinstance(kinds, list) and "integer" in kinds):
        yield where
    for key, item in node.items():
        if key not in {"examples", "example", "default"}:
            yield from _unbounded(item, f"{where}/{key}")


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


# Pydantic's own format names, which no validator reads, by the standard
# formats that say the same: an address of either IP version.
_STANDARD_FORMATS = {"ipvanyaddress": ("ipv4", "ipv6")}


def _standard_formats(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _standard_formats(item)
    elif isinstance(value, dict):
        if isinstance(said := value.get("format"), str) and (standard := _STANDARD_FORMATS.get(said)) is not None:
            del value["format"]
            value["anyOf"] = [{"format": name} for name in standard]
        for item in value.values():
            _standard_formats(item)


def _title_compositions(definitions: Mapping[str, JsonSchemaValue]) -> None:
    # A definition that is only a choice between shapes is an alias's: a class
    # always says its own shape. Its name is its declaration.
    for name, definition in definitions.items():
        if is_bare_composition(definition) and "title" not in definition:
            definition["title"] = _said(name)


def _title_branches(value: Any, definitions: Mapping[str, JsonSchemaValue]) -> None:
    # A branch naming a class's definition is titled beside its ``$ref``,
    # where the choice offers it: the definition itself stays untitled, filed
    # by name. A branch of a scalar alias is labeled by its type.
    if isinstance(value, list):
        for item in value:
            _title_branches(item, definitions)
    elif isinstance(value, dict):
        for branch in [*value.get("anyOf", ()), *value.get("oneOf", ())]:
            _title_branch(branch, definitions)
        for item in value.values():
            _title_branches(item, definitions)


def _title_branch(branch: Any, definitions: Mapping[str, JsonSchemaValue]) -> None:
    ref = branch.get("$ref", "") if isinstance(branch, dict) else ""
    name = ref.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
    if "properties" in definitions.get(name, {}) and "title" not in branch:
        branch["title"] = _said(name)


def _said(name: str) -> str:
    """A definition's name as words: ``AssetSaved`` is "Asset saved"."""

    return field_label(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower())


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
    dataclass as ``as_written`` writes it: a dict to the type checker and
    ``Kind`` to every validator."""

    def __init__(self, kind: type) -> None:
        self.kind = kind

    def __get_pydantic_core_schema__(self, _source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        schema = handler(self.kind)
        schema["metadata"] = {**schema.get("metadata", {}), _WRITTEN: True}
        return schema


def as_written(value: Any) -> dict[str, Any]:
    """``value``, a dataclass a ``Written`` part types, as the part is
    published: each field its type publishes (one marked ``Field(exclude=True)``
    left out, a ``computed_field`` property in), as JSON writes it."""

    written: dict[str, Any] = _writer(type(value)).dump_python(value, mode="json")
    return written


@functools.cache
def _writer(kind: type) -> TypeAdapter[Any]:
    return TypeAdapter(kind)


# When a dataclass a ``Written`` part types last read its source, written as
# ``ObservedAt`` says, as every answer writes a moment. An alias by assignment,
# so the field refers to ``ObservedAt`` itself.
ObservedMoment = Annotated[datetime, PlainSerializer(datetime.isoformat, return_type=ObservedAt, when_used="json")]


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


def sent_schema(kind: Any) -> dict[str, Any]:
    """The schema of ``kind``, something a caller sends: a command, a query, a tool's arguments, an identifier."""

    return json_schema(kind, generator=SentSchema)


def json_schemas(
    kinds: Mapping[str, Any], *, mode: JsonSchemaMode, ref_template: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Each of ``kinds``' schemas by its key, and the definitions they share by name.

    A type's schema is a function of the type alone, and building one is most
    of the cost of the API document, which a server and each test that checks
    a call against the document build whole; so each is built once a process
    and handed out as a copy its caller may change."""

    asked = tuple(kinds.items())
    try:
        built = _built(asked, mode, ref_template)
    except TypeError:
        # A type that cannot be a key (an annotation carrying a list) is built each time.
        built = _build(asked, mode, ref_template)
    return copy.deepcopy(built)


class ClashingDefinition(RuntimeError):
    """Two declarations define one name differently, so one document cannot carry both under it."""


def merged_definitions(into: dict[str, Any], definitions: Mapping[str, Any]) -> dict[str, Any]:
    """``into`` with each of ``definitions`` added: a name holds one meaning, kept once."""

    for name, definition in definitions.items():
        if into.setdefault(name, definition) != definition:
            raise ClashingDefinition(f"Two declarations define {name!r} differently: one name is one type.")
    return into


# Enough for every document a process builds: each holds about 170 sets of types.
_BUILT = 1024


@functools.lru_cache(maxsize=_BUILT)
def _built(
    kinds: tuple[tuple[str, Any], ...], mode: JsonSchemaMode, ref_template: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    return _build(kinds, mode, ref_template)


def _build(
    kinds: tuple[tuple[str, Any], ...], mode: JsonSchemaMode, ref_template: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    adapters = [(key, mode, TypeAdapter(kind)) for key, kind in kinds]
    refs, definitions = TypeAdapter.json_schemas(adapters, ref_template=ref_template, schema_generator=DocumentSchema)
    return {key: refs[(key, mode)] for key, _kind in kinds}, definitions.get("$defs", {})


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


def narrowed(schema: Mapping[str, Any], kept: Iterable[str]) -> dict[str, Any]:
    """``schema`` with only the properties ``kept``, and only the definitions they refer to."""

    wanted = set(kept)
    part = {key: value for key, value in schema.items() if key not in {"properties", "required", "$defs"}}
    part["properties"] = {name: value for name, value in schema.get("properties", {}).items() if name in wanted}
    if required := [name for name in schema.get("required", ()) if name in wanted]:
        part["required"] = required
    definitions = schema.get("$defs", {})
    referred: set[str] = set()
    pending: list[Any] = [part["properties"]]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            ref = value.get("$ref", "")
            if isinstance(ref, str) and ref.startswith(LOCAL) and (name := ref.removeprefix(LOCAL)) not in referred:
                referred.add(name)
                pending.append(definitions[name])
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if referred:
        part["$defs"] = {name: value for name, value in definitions.items() if name in referred}
    return part
