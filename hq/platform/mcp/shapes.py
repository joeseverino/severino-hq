"""What each MCP tool declares beside its handler: the title a person reads
it by, who may run it, and the query model of a read the API serves too.
What it answers is its handler's return annotation, the type the application
returns, listed as its
``outputSchema`` (docs/API.md says why every one is ``anyOf`` its success and
``REFUSED``).
"""

import inspect
from collections.abc import Callable, Mapping
from copy import copy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from hq.platform.application.access import Access
from hq.platform.application.json_schemas import LOCAL, AnswerSchema, PublishedSchema, json_schema
from hq.platform.application.refusal_status import RefusalEnvelope

type Handler = Callable[..., Any]
type Schema = dict[str, Any]
# Who may run a tool, decided in the application: a call is refused by it and
# tools/list leaves the tool out by it, so the two read one rule.
type Requirement = Access

# What a schema says in words: docs/API.md says what each field holds.
_WORDS = frozenset({"title", "description", "examples", "$defs"})
# The keywords whose value is a schema, a list of them, or a map of names to them.
_ONE = frozenset({"items", "additionalProperties", "not", "contains"})
_MANY = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_NAMED = frozenset({"properties", "patternProperties"})


def _bare(schema: Any, definitions: Mapping[str, Schema]) -> Any:
    """``schema`` as a document of its own: each ``$ref`` written out where it is
    used, without the words, and a type or null said as the one ``type`` list."""

    if not isinstance(schema, dict):
        return schema
    if ref := schema.get("$ref"):
        beside = {key: value for key, value in schema.items() if key != "$ref"}
        return _bare({**definitions[ref.removeprefix(LOCAL)], **beside}, definitions)
    kept: Schema = {}
    for key, value in schema.items():
        if key in _WORDS:
            continue
        if key in _ONE:
            kept[key] = _bare(value, definitions)
        elif key in _MANY:
            kept[key] = [_bare(item, definitions) for item in value]
        elif key in _NAMED:
            kept[key] = {name: _bare(item, definitions) for name, item in value.items()}
        else:
            kept[key] = value
    return _one_type(kept)


# Keywords that hold whatever the type, so a branch carrying one cannot take null into its ``type``.
_ANY_TYPE = frozenset({"const", "enum", "anyOf", "oneOf", "allOf", "not"})
_NULL: Schema = {"type": "null"}


def _one_type(schema: Schema) -> Schema:
    """The same schema in fewer bytes: a ``const`` without the ``type`` it
    already fixes, and ``anyOf`` a type and null as that type or null, since
    every other keyword of a typed schema leaves a null alone."""

    if "const" in schema and isinstance(schema.get("type"), str):
        return {key: value for key, value in schema.items() if key != "type"}
    branches = schema.get("anyOf")
    if schema.keys() != {"anyOf"} or not isinstance(branches, list) or len(branches) != 2 or _NULL not in branches:
        return schema
    (typed,) = (branch for branch in branches if branch != _NULL)
    if not isinstance(typed.get("type"), str) or typed.keys() & _ANY_TYPE:
        return schema
    return {**typed, "type": [typed["type"], "null"]}


def _schema(kind: Any, generator: type[PublishedSchema]) -> Schema:
    schema = json_schema(kind, mode="serialization", generator=generator)
    bare: Schema = _bare(schema, schema.get("$defs", {}))
    return bare


def answer_schema(kind: Any) -> Schema:
    """``kind``'s schema as a tool lists it: self-contained, since a tool's
    cannot reference another's, without the words, open but where ``kind``
    is closed, since a client validates each answer by it as it comes, and
    stating the default of each field an answer leaves out (``omitted``)."""

    listed: Schema = _defaulted(_schema(kind, AnswerSchema))
    return listed


class _Whole(PublishedSchema):
    """Every part of an answer typed, a ``Written`` one too, and open as an answer is."""

    closes_typed_dicts = False


def whole_schema(kind: Any) -> Schema:
    """``kind``'s schema as a tool would list it with each ``Written`` part
    typed whole, as the API document types it, before any default is stated."""

    return _schema(kind, _Whole)


def omission_schema(kind: Any) -> Schema:
    """What ``omitted`` reads ``kind``'s answers by: its whole schema, each
    default stated."""

    whole: Schema = _defaulted(whole_schema(kind))
    return whole


# What a field of each type holds when it says nothing: the default an answer
# leaves it out for. A number is a figure, so a zero is said.
_ZEROS: Mapping[str, Any] = frozendict({"string": "", "boolean": False, "array": [], "object": {}})
_NONE = object()


def _same(value: Any, default: Any) -> bool:
    # ``False == 0`` in Python; in JSON they are two values.
    return type(value) is type(default) and value == default


def _zero(field: Schema) -> Any:
    """The empty value ``field`` takes, or ``_NONE``: null where it takes null,
    else its type's own; none for a constant, a number, or a choice of types."""

    if "const" in field:
        return _NONE
    kind = field.get("type")
    if isinstance(kind, list):
        zero = None if "null" in kind else _NONE
    elif isinstance(kind, str):
        zero = _ZEROS.get(kind, _NONE)
    else:
        zero = None if _NULL in field.get("anyOf", ()) else _NONE
    if zero is not _NONE and "enum" in field and not any(_same(zero, value) for value in field["enum"]):
        return _NONE
    return zero


def _defaulted(schema: Any) -> Any:
    """``schema`` with each field that can be empty stating that empty value
    as its ``default``, and none of them ``required``: an answer leaves a
    field out that holds its default. A default the type declares stands."""

    if not isinstance(schema, dict):
        return schema
    for inner in _within(schema):
        _defaulted(inner)
    fields = schema.get("properties") or {}
    for field in fields.values():
        if "default" not in field and (zero := _zero(field)) is not _NONE:
            field["default"] = copy(zero)
    if (required := schema.get("required")) is not None:
        kept = [name for name in required if "default" not in fields.get(name, {})]
        if kept:
            schema["required"] = kept
        else:
            del schema["required"]
    return schema


def _within(schema: Schema) -> list[Any]:
    """Each schema ``schema`` holds directly."""

    found: list[Any] = []
    for key, value in schema.items():
        if key in _ONE:
            found.append(value)
        elif key in _MANY:
            found += value
        elif key in _NAMED:
            found += value.values()
    return found


def _shapes(schema: Schema) -> list[Schema]:
    """Each object shape ``schema`` may be, through its choices."""

    found = [schema] if "properties" in schema or isinstance(schema.get("additionalProperties"), dict) else []
    for key in _MANY:
        for branch in schema.get(key) or ():
            found += _shapes(branch)
    return found


def _items(schema: Schema) -> list[Schema]:
    """What each item of a list ``schema`` may be, through its choices."""

    found = [schema["items"]] if isinstance(schema.get("items"), dict) else []
    for key in _MANY:
        for branch in schema.get(key) or ():
            found += _items(branch)
    return found


def _either(schemas: list[Schema]) -> Schema:
    return schemas[0] if len(schemas) == 1 else {"anyOf": schemas}


def omitted(value: Any, schema: Schema) -> Any:
    """``value``, a JSON answer, without each field that holds the default
    ``schema`` states for it in every shape that names it. A field the schema
    does not name, and a map's entries, are left as they are, so an untyped
    part (a record, a JSON Schema) is never read as having defaults."""

    if isinstance(value, list):
        items = _items(schema)
        return [omitted(item, _either(items)) for item in value] if items else value
    if not isinstance(value, dict):
        return value
    shapes = _shapes(schema)
    return _omitted_fields(value, shapes) if shapes else value


def _omitted_fields(value: dict[str, Any], shapes: list[Schema]) -> dict[str, Any]:
    """``value``, an object one of ``shapes``, without each field at its default."""

    entries = [shape["additionalProperties"] for shape in shapes if isinstance(shape.get("additionalProperties"), dict)]
    kept: dict[str, Any] = {}
    for key, inner in value.items():
        named = [shape["properties"][key] for shape in shapes if key in (shape.get("properties") or {})]
        if not _at_default(inner, named):
            within = named or entries
            kept[key] = omitted(inner, _either(within)) if within else inner
    return kept


def _at_default(value: Any, named: list[Schema]) -> bool:
    """Whether ``value`` is the default every field schema that names it states."""

    return bool(named) and all("default" in field and _same(value, field["default"]) for field in named)


def answer_type(handler: Handler) -> Any:
    """What ``handler`` answers when it succeeds: its return annotation."""

    answer = inspect.signature(handler, eval_str=True).return_annotation
    if answer is inspect.Signature.empty:
        raise ValueError(f"MCP tool {handler.__name__!r} must annotate what it answers.")
    return answer


# A refusal is answered whole, so its schema states no default.
REFUSED: Schema = _schema(RefusalEnvelope, AnswerSchema)


def output_schema(success: Schema) -> Schema:
    """What a call's ``structuredContent`` meets: the tool's success, or a refusal."""

    # An object at the root, as protocols through 2025-11-25 require of an outputSchema.
    return {"type": "object", "anyOf": [success, REFUSED]}


@dataclass(frozen=True, slots=True)
class Budget:
    """The most bytes a tool's answer to ``call``, its arguments, may take on
    the standard data set, composed. A byte count does not move with load, so
    it is held exactly (``test_answer_budgets``), and ``audit:mcp`` makes the
    same call."""

    bytes: int
    call: Mapping[str, Any] = frozendict()


@dataclass(frozen=True, slots=True)
class Declared:
    title: str
    requires: Requirement
    budget: Budget
    # The read's own query model, which the API's query is read by too.
    arguments: type[BaseModel] | None = None


_DECLARED: dict[Handler, Declared] = {}


def tool[F: Handler](
    title: str, *, requires: Requirement, budget: Budget, arguments: type[BaseModel] | None = None
) -> Callable[[F], F]:
    """Declare a handler a tool: ``title`` is what a client shows a person for
    it, ``requires`` who may run it, which every call is refused by and
    tools/list is narrowed by, and ``budget`` what its answer may cost. A read
    the API serves too names its query model as ``arguments``, which validates
    its arguments in place of its signature and calls the handler with them
    field by field."""

    def declare(handler: F) -> F:
        _DECLARED[handler] = Declared(title, requires, budget, arguments)
        return handler

    return declare


def declared(handler: Handler) -> Declared:
    """What ``handler`` declared; one never declared is refused, since a client shows a person each tool by its title."""

    try:
        return _DECLARED[handler]
    except KeyError:
        raise ValueError(f"MCP tool {handler.__name__!r} must be declared with shapes.tool.") from None
