"""Bind documented MCP arguments to the declared application adapters."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from asgiref.sync import sync_to_async
from pydantic import BaseModel

from .contract import resource_contract
from .declarations import HANDLERS, argument_model


@dataclass(frozen=True, slots=True)
class Tool:
    """One MCP tool: what is advertised, and how a call reaches its handler."""

    name: str
    description: str
    # The document's schema, generated from ``arguments``; `mise run drift`
    # holds the written document to that source.
    input_schema: Mapping[str, Any]
    # The protocol's tool annotations, as the document states them.
    annotations: Mapping[str, bool]
    arguments: type[BaseModel]
    handler: Callable[..., Any]
    execute: Callable[..., Any] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "execute", sync_to_async(self.handler, thread_sensitive=True))

    @property
    def output_schema(self) -> dict[str, Any]:
        # Every handler answers with one JSON object.
        return {"additionalProperties": True, "title": f"{self.name}DictOutput", "type": "object"}

    async def call(self, arguments: Mapping[str, Any]) -> Any:
        """The handler's answer, once the arguments pass its model, whose
        refusal never quotes the input."""

        bound = self.arguments.model_validate(dict(arguments))
        return await self.execute(**{name: getattr(bound, name) for name in type(bound).model_fields})


def bind_tools(document: dict[str, Any]) -> dict[str, Tool]:
    """One tool for each declared handler the document describes."""

    entries = document.get("x-hq-mcp-tools")
    if not isinstance(entries, dict) or set(entries) != {handler.__name__ for handler in HANDLERS}:
        raise ValueError("The document must describe exactly the declared MCP handlers.")
    resources = resource_contract(document)
    tools: dict[str, Tool] = {}
    for handler in HANDLERS:
        name = handler.__name__
        entry = entries[name]
        description = entry["description"]
        if not isinstance(description, str) or not description:
            raise ValueError(f"MCP tool {name!r} must have a description.")
        annotations = entry.get("annotations")
        if not isinstance(annotations, dict) or not all(isinstance(value, bool) for value in annotations.values()):
            raise ValueError(f"MCP tool {name!r} must state its annotations.")
        arguments = argument_model(handler, resources)
        tools[name] = Tool(
            name, description, deepcopy(entry["inputSchema"]), frozendict(annotations), arguments, handler
        )
    return tools
