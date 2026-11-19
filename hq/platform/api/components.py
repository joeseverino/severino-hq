"""The API document's components/schemas: each schema kept once under its name, its local definitions hoisted.

``openapi`` builds the document; this is where a schema it names lands.
"""

import copy
from collections.abc import Iterator
from typing import Any

from hq.platform.application.json_schemas import LOCAL, json_schemas, merged_definitions

SCHEMAS = "#/components/schemas/"
# A component's copy for an answer a field mask cuts, suffixed as Pydantic names a mode's schema.
PARTIAL = "Partial"
# What a cut answer can lack: a field, or the one a discriminator reads.
_PROMISES = ("required", "discriminator", "oneOf")


class Components:
    """components/schemas, filled once per name."""

    def __init__(self) -> None:
        self.schemas: dict[str, Any] = {}
        # The components a caller's field mask cuts, each a record or a row.
        self.masked: set[str] = set()

    def mask(self, name: str) -> dict[str, Any]:
        """The ``$ref`` an answer a caller's field mask cuts makes to the component ``name``."""

        self.masked.add(name)
        return {"$ref": SCHEMAS + name + PARTIAL}

    def written(self) -> dict[str, Any]:
        """The schemas, and a field mask's reach copied as ``<Name>Partial``, which promises no
        field (docs/API.md, "What each surface exposes"), lending each described part it keeps."""

        reached: set[str] = set()
        waiting = list(self.masked)
        while waiting:
            if (name := waiting.pop()) not in reached:
                reached.add(name)
                waiting.extend(_referred(self.schemas[name]))
        # A component that promises nothing, and refers to none that does, needs no copy.
        partial = self.masked | {name for name in reached if _promises(self.schemas[name])}
        while grown := {name for name in reached - partial if _referred(self.schemas[name]) & partial}:
            partial |= grown
        written = dict(self.schemas)
        for name in partial:
            merged_definitions(written, {name + PARTIAL: _relaxed(self.schemas[name], partial, SCHEMAS + name)})
        return written

    def typed(self, types: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Schemas for Python types, their definitions hoisted into components."""

        refs, definitions = json_schemas(types, mode="serialization", ref_template=SCHEMAS + "{model}")
        for name, schema in definitions.items():
            self.add(name, schema)
        return refs

    def add(self, name: str, schema: dict[str, Any]) -> str:
        merged_definitions(self.schemas, {name: schema})
        return SCHEMAS + name

    def hoist(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Move a JSON Schema's local ``$defs`` into components, each under its own name.

        Inside an OpenAPI document ``#/$defs/X`` resolves against the document,
        not the schema. A definition is named for the type it is, so one alias
        used by many schemas is one component, and two that mean different
        things under one name fail in ``merged_definitions``.
        """

        for key, definition in schema.get("$defs", {}).items():
            self.add(key, _rewritten(definition))
        rewritten: dict[str, Any] = _rewritten(schema)
        return rewritten


def _rewritten(value: Any) -> Any:
    """``value`` with each local ``$ref`` pointing at the component of the same name, its ``$defs`` gone."""

    if isinstance(value, dict):
        return {
            key: (
                SCHEMAS + item.removeprefix(LOCAL)
                if key == "$ref" and isinstance(item, str) and item.startswith(LOCAL)
                else _rewritten(item)
            )
            for key, item in value.items()
            if key != "$defs"
        }
    if isinstance(value, list):
        return [_rewritten(item) for item in value]
    return value


def _promises(schema: Any) -> bool:
    """Whether a cut answer could lack what ``schema`` or a part inside it promises, short of a ``$ref``'s target."""

    return any(key in schema for key in _PROMISES) or any(_promises(part) for *_, part in _parts(schema))


def _relaxed(schema: Any, partial: set[str], at: str) -> Any:
    """``schema``, which is at ``at``, promising no field, each ``$ref`` to a component in ``partial`` to its copy."""

    if "description" in schema and not _promises(schema) and not _referred(schema) & partial:
        return {"$ref": at}
    relaxed = {key: copy.copy(value) for key, value in schema.items() if key not in {"required", "discriminator"}}
    named, _, inside = schema.get("$ref", "").removeprefix(SCHEMAS).partition("/")
    if named in partial:
        relaxed["$ref"] = SCHEMAS + named + PARTIAL + (inside and "/" + inside)
    for key, item, part in _parts(schema):
        pointer = at + "/" + key + ("" if item is None else "/" + str(item).replace("~", "~0").replace("/", "~1"))
        if item is None:
            relaxed[key] = _relaxed(part, partial, pointer)
        else:
            relaxed[key][item] = _relaxed(part, partial, pointer)
    # A cut object may lack the key a ``oneOf``'s branches are told apart by.
    if "oneOf" in relaxed:
        relaxed["anyOf"] = relaxed.pop("oneOf")
    return relaxed


def _parts(schema: dict[str, Any]) -> Iterator[tuple[str, str | int | None, dict[str, Any]]]:
    """Each subschema directly inside ``schema``: the keyword it is under, its name or place there, and itself."""

    for key in ("properties", "patternProperties"):
        yield from ((key, name, part) for name, part in schema.get(key, {}).items())
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            yield key, None, schema[key]
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        yield from ((key, index, part) for index, part in enumerate(schema.get(key, [])))


def _referred(value: Any) -> set[str]:
    """The components ``value`` refers to, wholly or in part, wherever in it."""

    if isinstance(value, dict):
        ref = value.get("$ref", "")
        named = {ref.removeprefix(SCHEMAS).partition("/")[0]} if ref.startswith(SCHEMAS) else set()
        return named.union(*map(_referred, value.values()))
    if isinstance(value, list):
        return set().union(*map(_referred, value))
    return set()
