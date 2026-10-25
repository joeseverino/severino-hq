"""The MCP handler set and the arguments each is called with, without a server dependency."""

import inspect
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model

from . import services
from .contract import Resource, ResourceContract, catalog, resource_contract

HANDLERS: tuple[Callable[..., Any], ...] = (
    services.describe_capabilities,
    services.execute_capability,
    services.describe_resources,
    services.describe_connections,
    services.list_connections,
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


def hints(handler: Callable[..., Any]) -> dict[str, bool]:
    """What a client may assume of a call: the protocol's tool annotations."""

    if handler in CHANGES:
        return {"readOnlyHint": False}
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
    return None


def argument_model(handler: Callable[..., Any], resources: ResourceContract) -> type[BaseModel]:
    """The handler's parameters as the one model its arguments are validated by
    and its published schema is generated from. A resource tool's ``name`` is
    one of the resources the document serves for it."""

    kinds = _kinds(handler, resources)
    fields: dict[str, Any] = {
        name: (
            Any if parameter.annotation is inspect.Parameter.empty else parameter.annotation,
            ... if parameter.default is inspect.Parameter.empty else parameter.default,
        )
        for name, parameter in inspect.signature(handler, eval_str=True).parameters.items()
    }
    # A Literal needs one value; with no resource to name, the service refuses every name.
    if kinds:
        fields["name"] = (Literal[tuple(resource.name for resource in kinds)], ...)
    return create_model(f"{handler.__name__}Arguments", __config__=_ARGUMENTS, **fields)


def tool_contract(document: dict[str, Any]) -> dict[str, Any]:
    """MCP-only metadata; these tools are not synthetic HTTP operations."""

    resources = resource_contract(document)
    entries: dict[str, Any] = {}
    for handler in HANDLERS:
        name = handler.__name__
        schema = argument_model(handler, resources).model_json_schema(by_alias=True)
        description = handler.__doc__ or ""
        if (kinds := _kinds(handler, resources)) is not None:
            description += "\nKinds:\n" + catalog(kinds)
        entries[name] = {"description": description, "inputSchema": schema, "annotations": hints(handler)}
    if len(entries) != len(HANDLERS):
        raise ValueError("MCP handler names must be unique.")
    return entries
