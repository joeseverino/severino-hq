"""What each MCP tool declares beside its handler: the title a person reads
it by, and the choices its text parameters take. What it answers is its
handler's return annotation, the type the application returns, listed as its
``outputSchema`` (docs/API.md says why every one is ``anyOf`` its success and
``REFUSED``).
"""

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from hq.platform.application.json_schemas import LOCAL, AnswerSchema, json_schema
from hq.platform.application.refusal_status import RefusalEnvelope

type Handler = Callable[..., Any]
type Schema = dict[str, Any]

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


def answer_schema(kind: Any) -> Schema:
    """``kind``'s schema as a tool lists it: self-contained, since a tool's
    cannot reference another's, without the words, and open but where ``kind``
    is closed, since a client validates each answer by it as it comes."""

    schema = json_schema(kind, mode="serialization", generator=AnswerSchema)
    bare: Schema = _bare(schema, schema.get("$defs", {}))
    return bare


def answer_type(handler: Handler) -> Any:
    """What ``handler`` answers when it succeeds: its return annotation."""

    answer = inspect.signature(handler, eval_str=True).return_annotation
    if answer is inspect.Signature.empty:
        raise ValueError(f"MCP tool {handler.__name__!r} must annotate what it answers.")
    return answer


REFUSED: Schema = answer_schema(RefusalEnvelope)


def output_schema(success: Schema) -> Schema:
    """What a call's ``structuredContent`` meets: the tool's success, or a refusal."""

    # An object at the root, as protocols through 2025-11-25 require of an outputSchema.
    return {"type": "object", "anyOf": [success, REFUSED]}


type Choices = Mapping[str, Callable[[], Iterable[str]]]


@dataclass(frozen=True, slots=True)
class Declared:
    title: str
    # Each text parameter that takes only some values, and where they are declared.
    choices: Choices


_DECLARED: dict[Handler, Declared] = {}


def tool[F: Handler](title: str, *, choices: Choices | None = None) -> Callable[[F], F]:
    """Declare a handler a tool: ``title`` is what a client shows a person for
    it, and ``choices`` the values each named parameter may take, read from
    where they are declared."""

    def declare(handler: F) -> F:
        _DECLARED[handler] = Declared(title, choices or {})
        return handler

    return declare


def declared(handler: Handler) -> Declared:
    """What ``handler`` declared; one never declared is refused, since a client shows a person each tool by its title."""

    try:
        return _DECLARED[handler]
    except KeyError:
        raise ValueError(f"MCP tool {handler.__name__!r} must be declared with shapes.tool.") from None
