"""The MCP handler set and the arguments each is called with, without a server dependency."""

import inspect
from collections.abc import Callable
from functools import cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model

from hq.platform.application.json_schemas import json_schema

from . import services
from .contract import Resource, ResourceContract, filters, identifiers, served_resources
from .shapes import answer_schema, answer_type, declared, output_schema

HANDLERS: tuple[Callable[..., Any], ...] = (
    services.describe_capabilities,
    services.execute_capability,
    services.describe_resources,
    services.describe_connections,
    services.get_topology,
    services.get_findings,
    services.list_resource,
    services.get_resource,
    services.audit_registry,
    services.export_year_summary,
    services.documentation_status,
    services.recent_activity,
    services.system_health,
    services.dashboard_snapshot,
)

# The one tool that can change anything: it runs a capability, and a capability
# may write or reach a provider. Every other tool reads what HQ already holds.
CHANGES = frozenset({services.execute_capability})
# The tools that read and record that they read: an export is written to the
# audit log, so a call adds a row and changes nothing else.
RECORDS = frozenset({services.export_year_summary})


def hints(handler: Callable[..., Any]) -> dict[str, bool]:
    """What a client may assume of a call: the protocol's tool annotations.

    The title is the tool's own field, which a client shows before
    ``annotations.title``, so it is not repeated here.
    """

    if handler in CHANGES:
        # A capability may delete or replace what it acts on, and reach a
        # provider. Repeating a call is safe because every one that changes
        # anything carries a retry key, and a repeat with that key returns
        # the first answer.
        return {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True}
    if handler in RECORDS:
        # Asking again adds another row of the same export and touches nothing else.
        return {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    return {"readOnlyHint": True, "openWorldHint": False}


# Closed and strict: an argument the handler does not take, or one of the wrong
# type, is refused rather than dropped or coerced, and the refusal never quotes
# what was sent.
_ARGUMENTS = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


def _kinds(handler: Callable[..., Any], resources: ResourceContract) -> tuple[Resource, ...] | None:
    """The resources a tool names by its ``name`` argument, when it names one."""

    if handler is services.list_resource:
        return resources.listable
    if handler is services.get_resource:
        return resources.addressable
    if handler is services.describe_resources:
        return resources.described
    return None


@cache
def argument_model(handler: Callable[..., Any], resources: ResourceContract) -> type[BaseModel]:
    """The handler's parameters as the one model its arguments are validated by
    and its published schema is generated from, built once for the contract
    both read. A resource tool's ``name`` is one of the resources the registry
    serves for it. A read that declares its query model is validated by that
    model itself."""

    if (query := declared(handler).arguments) is not None:
        return query
    parameters = inspect.signature(handler, eval_str=True).parameters
    fields: dict[str, Any] = {
        name: (
            Any if parameter.annotation is inspect.Parameter.empty else parameter.annotation,
            ... if parameter.default is inspect.Parameter.empty else parameter.default,
        )
        for name, parameter in parameters.items()
    }
    # A Literal needs one value; with none to name, the service refuses every value.
    if (kinds := _kinds(handler, resources)) is not None and (names := tuple(resource.name for resource in kinds)):
        default = parameters["name"].default
        chosen: Any = Literal[names]
        fields["name"] = (chosen, ...) if default is inspect.Parameter.empty else (chosen | None, default)
    return create_model(f"{handler.__name__}Arguments", __config__=_ARGUMENTS, **fields)


def tool_contract() -> dict[str, Any]:
    """Each tool as a client is told of it: its title, description, arguments' and answer's schemas
    and annotations."""

    resources = served_resources()
    entries: dict[str, Any] = {}
    for handler in HANDLERS:
        name = handler.__name__
        schema = json_schema(argument_model(handler, resources))
        # The docstring is the description a model reads, so it is Markdown:
        # a one-paragraph summary first, the rest in paragraphs after a blank
        # line, and names in single backticks.
        description = handler.__doc__ or ""
        if not description.strip():
            # A client chooses a tool by its description; one with none is never chosen right.
            raise ValueError(f"MCP tool {name!r} must have a description.")
        if handler is services.list_resource:
            description += "\nFilters: " + filters(resources.listable)
        if handler is services.get_resource:
            description += "\nIdentifiers: " + identifiers(resources.addressable)
        declaration = declared(handler)
        entries[name] = {
            "title": declaration.title,
            "description": description,
            "inputSchema": schema,
            "outputSchema": output_schema(answer_schema(answer_type(handler))),
            "annotations": hints(handler),
        }
    if len(entries) != len(HANDLERS):
        raise ValueError("MCP handler names must be unique.")
    return entries
