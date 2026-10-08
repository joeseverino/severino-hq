"""Bind documented MCP arguments to the declared application adapters."""

from collections.abc import Callable
from copy import deepcopy
from functools import wraps
from typing import Any

from asgiref.sync import sync_to_async

# MCP's existing jsonschema dependency ships without inline type information.
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]
from mcp.server.fastmcp import FastMCP

from .declarations import HANDLERS, argument_metadata


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


def _bound(handler: Callable[..., Any], schema: dict[str, Any]) -> Callable[..., Any]:
    validator = Draft202012Validator(schema)
    execute = sync_to_async(handler, thread_sensitive=True)

    @wraps(handler)
    async def call(**arguments: Any) -> Any:
        # FastMCP disables low-level schema validation. Validate the document's
        # constraints here before crossing into authenticated application code.
        try:
            validator.validate(arguments)
        except ValidationError:
            # jsonschema's default message includes the rejected input value.
            raise ValueError("MCP arguments do not match the documented input schema.") from None
        return await execute(**arguments)

    return call


def register_tools(destination: FastMCP, document: dict[str, Any]) -> None:
    """Use one canonical document snapshot; fail startup on signature drift."""

    entries = document.get("x-hq-mcp-tools")
    if not isinstance(entries, dict) or set(entries) != {handler.__name__ for handler in HANDLERS}:
        raise ValueError("The document must describe exactly the declared MCP handlers.")
    prepared = []
    for handler in HANDLERS:
        name = handler.__name__
        entry = entries[name]
        schema = deepcopy(entry["inputSchema"])
        description = entry["description"]
        if not isinstance(description, str) or not description:
            raise ValueError(f"MCP tool {name!r} must have a description.")
        Draft202012Validator.check_schema(schema)
        metadata = argument_metadata(handler)
        expected = metadata.arg_model.model_json_schema(by_alias=True)
        if _signature_shape(schema) != _signature_shape(expected):
            raise ValueError(f"MCP tool {name!r} schema disagrees with its execution signature.")
        prepared.append((handler, schema, description, metadata))
    for handler, schema, description, metadata in prepared:
        destination.add_tool(_bound(handler, schema), description=description)
        # FastMCP exposes no input-schema override in add_tool. Keep the SDK
        # seam here: its Tool model owns advertisement and argument execution.
        tool = destination._tool_manager.get_tool(handler.__name__)
        if tool is None:
            raise RuntimeError("The SDK did not register the declared MCP tool.")
        tool.parameters = schema
        tool.fn_metadata = metadata
