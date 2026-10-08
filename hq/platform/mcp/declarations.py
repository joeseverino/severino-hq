"""The MCP handler set and signature metadata, without a server dependency."""

from collections.abc import Callable
from typing import Any, override

from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata, func_metadata
from pydantic import ConfigDict

from . import services
from .contract import catalogue, resource_contract

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


class StrictMetadata(FuncMetadata):
    @override
    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        # The canonical schema describes JSON objects, not JSON inside strings.
        return data


def argument_metadata(handler: Callable[..., Any]) -> FuncMetadata:
    """Use the SDK's signature model with strict, closed arguments."""

    metadata = func_metadata(handler)
    model = metadata.arg_model
    model.model_config = ConfigDict(
        **{**model.model_config, "extra": "forbid", "strict": True, "hide_input_in_errors": True}
    )
    model.model_rebuild(force=True)
    return StrictMetadata(
        arg_model=model,
        output_schema=metadata.output_schema,
        output_model=metadata.output_model,
        wrap_output=metadata.wrap_output,
    )


def tool_contract(document: dict[str, Any]) -> dict[str, Any]:
    """MCP-only metadata; these tools are not synthetic HTTP operations."""

    resources = resource_contract(document)
    entries: dict[str, Any] = {}
    for handler in HANDLERS:
        name = handler.__name__
        schema = argument_metadata(handler).arg_model.model_json_schema(by_alias=True)
        description = handler.__doc__ or ""
        if name in {"list_resource", "get_resource"}:
            kinds = resources.listable if name == "list_resource" else resources.addressable
            schema["properties"]["name"]["enum"] = [resource.name for resource in kinds]
            description += "\nKinds:\n" + catalogue(kinds)
        entries[name] = {"description": description, "inputSchema": schema}
    if len(entries) != len(HANDLERS):
        raise ValueError("MCP handler names must be unique.")
    return entries
