"""Bind documented MCP arguments to the declared application adapters."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from asgiref.sync import sync_to_async
from jsonschema import Draft202012Validator, ValidationError
from pydantic import BaseModel

from .declarations import HANDLERS, argument_model


def _signature_shape(schema: Any) -> Any:
    """Compare execution types/defaults, allowing documented constraints."""

    if isinstance(schema, list):
        return [_signature_shape(value) for value in schema]
    if not isinstance(schema, dict):
        return schema
    return {
        key: (
            {name: _signature_shape(value) for name, value in item.items()}
            if key in {"properties", "$defs"}
            else _signature_shape(item)
        )
        for key, item in schema.items()
        if key
        in {
            "type",
            "anyOf",
            "oneOf",
            "items",
            "properties",
            "required",
            "additionalProperties",
            "$ref",
            "$defs",
            "default",
        }
    }


@dataclass(frozen=True, slots=True)
class Tool:
    """One MCP tool: what is advertised, and how a call reaches its handler."""

    name: str
    description: str
    # The document's schema, advertised and enforced; the model is the
    # handler's own signature, which the schema must agree with.
    input_schema: Mapping[str, Any]
    # The protocol's tool annotations, as the document states them.
    annotations: Mapping[str, bool]
    arguments: type[BaseModel]
    handler: Callable[..., Any]
    validator: Draft202012Validator = field(init=False)
    execute: Callable[..., Any] = field(init=False)

    def __post_init__(self) -> None:
        # Built once: the schema's validator, and the handler on a worker thread.
        object.__setattr__(self, "validator", Draft202012Validator(dict(self.input_schema)))
        object.__setattr__(self, "execute", sync_to_async(self.handler, thread_sensitive=True))

    @property
    def output_schema(self) -> dict[str, Any]:
        # Every handler answers with one JSON object.
        return {"additionalProperties": True, "title": f"{self.name}DictOutput", "type": "object"}

    async def call(self, arguments: Mapping[str, Any]) -> Any:
        """The handler's answer, once the arguments pass its signature and then
        the document's constraints. Neither refusal quotes the input."""

        bound = self.arguments.model_validate(dict(arguments))
        try:
            self.validator.validate(dict(arguments))
        except ValidationError:
            # jsonschema's default message includes the rejected input value.
            raise ValueError("MCP arguments do not match the documented input schema.") from None
        return await self.execute(**{name: getattr(bound, name) for name in type(bound).model_fields})


def bind_tools(document: dict[str, Any]) -> dict[str, Tool]:
    """One canonical document snapshot; startup fails on signature drift."""

    entries = document.get("x-hq-mcp-tools")
    if not isinstance(entries, dict) or set(entries) != {handler.__name__ for handler in HANDLERS}:
        raise ValueError("The document must describe exactly the declared MCP handlers.")
    tools: dict[str, Tool] = {}
    for handler in HANDLERS:
        name = handler.__name__
        entry = entries[name]
        schema = deepcopy(entry["inputSchema"])
        description = entry["description"]
        if not isinstance(description, str) or not description:
            raise ValueError(f"MCP tool {name!r} must have a description.")
        Draft202012Validator.check_schema(schema)
        annotations = entry.get("annotations")
        if not isinstance(annotations, dict) or not all(isinstance(value, bool) for value in annotations.values()):
            raise ValueError(f"MCP tool {name!r} must state its annotations.")
        arguments = argument_model(handler)
        if _signature_shape(schema) != _signature_shape(arguments.model_json_schema(by_alias=True)):
            raise ValueError(f"MCP tool {name!r} schema disagrees with its execution signature.")
        tools[name] = Tool(name, description, schema, frozendict(annotations), arguments, handler)
    return tools
