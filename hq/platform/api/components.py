"""The API document's components/schemas: each schema kept once under its name, its local definitions hoisted.

``openapi`` builds the document; this is where a schema it names lands.
"""

from typing import Any

from hq.platform.application.json_schemas import LOCAL, json_schemas

from .fragments import OpenAPIError

SCHEMAS = "#/components/schemas/"


class Components:
    """components/schemas, filled once per name."""

    def __init__(self) -> None:
        self.schemas: dict[str, Any] = {}
        # The components a caller's field mask may trim, which promise no field.
        self.masked: set[str] = set()

    def mask(self, name: str) -> None:
        """Promise no field of the component ``name``, whichever schema writes it first."""

        self.masked.add(name)
        if name in self.schemas:
            self.schemas[name].pop("required", None)

    def typed(self, types: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Schemas for Python types, their definitions hoisted into components."""

        refs, definitions = json_schemas(types, mode="serialization", ref_template=SCHEMAS + "{model}")
        for name, schema in definitions.items():
            self.add(name, schema)
        return refs

    def add(self, name: str, schema: dict[str, Any]) -> str:
        if name in self.masked:
            schema = {key: value for key, value in schema.items() if key != "required"}
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
