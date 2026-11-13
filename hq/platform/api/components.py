"""The API document's components/schemas: each schema kept once under its name, its local definitions hoisted.

``openapi`` builds the document; this is where a schema it names lands.
"""

import copy
from typing import Any

from hq.platform.application.json_schemas import LOCAL, json_schemas

from .fragments import OpenAPIError

SCHEMAS = "#/components/schemas/"


class Components:
    """components/schemas, filled once per name."""

    def __init__(self) -> None:
        self.schemas: dict[str, Any] = {}
        # The components a caller's field mask cuts, each a record or a row.
        self.masked: set[str] = set()

    def mask(self, name: str) -> None:
        """Name the component ``name`` as one a caller's field mask cuts."""

        self.masked.add(name)

    def sparse(self) -> dict[str, Any]:
        """The schemas, each part a field mask can reach promising no field.

        A mask keeps any field at any depth (``health.state``), and OpenAPI
        has no way to say an answer's shape follows a query parameter, so a
        part a mask reaches lists no ``required`` key; a whole answer still
        validates against it. Its parts are reached through every subschema
        and ``$ref``, so a part shared with an answer no mask cuts promises no
        field there either. A ``oneOf`` reached becomes ``anyOf``: its
        branches told apart by a required key could all match a cut object.
        """

        schemas = copy.deepcopy(self.schemas)
        reached: set[int] = set()

        def reach(schema: Any) -> None:
            if not isinstance(schema, dict) or id(schema) in reached:
                return
            reached.add(id(schema))
            schema.pop("required", None)
            if "oneOf" in schema:
                schema["anyOf"] = schema.pop("oneOf")
            if (ref := schema.get("$ref", "")).startswith(SCHEMAS):
                reach(_pointed(schemas, ref.removeprefix(SCHEMAS)))
            for key in ("properties", "patternProperties"):
                for part in schema.get(key, {}).values():
                    reach(part)
            for key in ("items", "additionalProperties"):
                reach(schema.get(key))
            for key in ("anyOf", "allOf", "prefixItems"):
                for part in schema.get(key, []):
                    reach(part)

        for name in sorted(self.masked):
            reach(schemas[name])
        return schemas

    def typed(self, types: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Schemas for Python types, their definitions hoisted into components."""

        refs, definitions = json_schemas(types, mode="serialization", ref_template=SCHEMAS + "{model}")
        for name, schema in definitions.items():
            self.add(name, schema)
        return refs

    def add(self, name: str, schema: dict[str, Any]) -> str:
        if self.schemas.setdefault(name, schema) != schema:
            raise OpenAPIError(
                f"Two schemas claim the component name {name!r}: each meaning is a type alias of its own name."
            )
        return SCHEMAS + name

    def hoist(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Move a JSON Schema's local ``$defs`` into components, each under its own name.

        Inside an OpenAPI document ``#/$defs/X`` resolves against the document,
        not the schema. A definition is named for the type it is, so one alias
        used by many schemas is one component, and two that mean different
        things under one name fail in ``add``.
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


def _pointed(schemas: dict[str, Any], pointer: str) -> Any:
    """The part of ``schemas`` a reference past ``SCHEMAS`` names: a component, or a part inside one."""

    value: Any = schemas
    for token in pointer.split("/"):
        value = value[token.replace("~1", "/").replace("~0", "~")]
    return value
