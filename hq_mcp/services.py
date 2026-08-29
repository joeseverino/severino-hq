"""Thin MCP adapters over HQ's canonical application services and safe queries."""

from __future__ import annotations

from typing import Any

from application.dashboard import operating_snapshot
from application.connections import (
    describe_connections as describe_application_connections,
)
from application.connections import list_connections as list_application_connections
from application.capabilities import (
    describe_capabilities as describe_application_capabilities,
)
from application.capabilities import (
    execute_capability as execute_application_capability,
)
from application.security import mcp_principal
from application.findings import findings as application_findings
from application.topology import topology as application_topology
from application.registry import audit_registry as audit_application_registry
from application.resources import (
    ResourceNotFound,
    describe_resources as describe_application_resources,
    get_resource as get_application_resource,
    list_resource as list_application_resource,
)
from application import read_models
from application.reports import export_year_summary as export_application_year_summary


class NotFoundError(ValueError):
    """A requested HQ object does not exist."""


def _write(service, command, **kwargs):
    """Invoke one application mutation as the authenticated MCP principal."""

    return service(command, principal=mcp_principal(), **kwargs)


def describe_capabilities() -> dict[str, Any]:
    """Describe every JSON-executable HQ capability and its canonical schema."""

    return describe_application_capabilities()


def execute_capability(
    name: str,
    payload: dict[str, Any],
    target: str | int | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Execute one allowlisted capability from a schema-validated JSON payload."""

    return execute_application_capability(
        name,
        payload,
        principal=mcp_principal(),
        target=target,
        expected_updated_at=expected_updated_at,
    )


def describe_resources() -> dict[str, Any]:
    """Describe every readable HQ resource and its supported operations."""

    return describe_application_resources()


def describe_connections() -> dict[str, Any]:
    """Describe installed connection families, abilities, scopes, and routes."""

    return describe_application_connections()


def list_connections() -> dict[str, Any]:
    """List safe cached connection state available to the MCP principal."""

    return list_application_connections(principal=mcp_principal())


def get_findings(rule: str = "") -> dict[str, Any]:
    """What HQ currently claims is wrong, with evidence and suggested remedies.

    Each remedy names an existing capability and target; run one with
    `execute_capability`. A remedy absent means this principal cannot run it.
    """

    return application_findings(principal=mcp_principal(), rule=rule.strip())


def get_topology(
    lens: str = "",
    focus: str = "",
    direction: str = "both",
    depth: int = 2,
) -> dict[str, Any]:
    """Return the live infrastructure graph with safe canonical actions.

    Pass a lens name for a standing question. To inspect dependencies or blast
    radius, pass a node id as ``focus`` and trace ``inbound``, ``outbound``, or
    ``both`` up to five hops. Every node retains its canonical safe actions.
    """

    return application_topology(
        principal=mcp_principal(),
        lens=lens.strip(),
        focus=focus.strip(),
        direction=direction.strip(),
        depth=depth,
    )


def list_resource(
    name: str, filters: dict[str, Any] | None = None
) -> dict[str, Any]:
    """List any registered resource with schema-validated filters."""

    return list_application_resource(
        name, filters, principal=mcp_principal(), strict=True
    )


def get_resource(name: str, identifier: str | int) -> dict[str, Any]:
    """Get one record from any registered addressable resource."""

    try:
        return get_application_resource(
            name, identifier, principal=mcp_principal(), strict=True
        )
    except ResourceNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def audit_registry() -> dict[str, Any]:
    """Report Project and Asset rows with no documentation references."""

    return audit_application_registry()


def export_year_summary(year: int, format: str = "md") -> dict[str, Any]:
    """Export one safe year summary as Markdown or JSON."""

    return export_application_year_summary(year, format, principal=mcp_principal())


def list_projects(
    *, status: str | None = None, query: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List HQ projects, optionally filtered by exact status or text search."""
    return list_resource(
        "projects", {"status": status, "query": query, "limit": limit}
    )


def get_project(slug: str) -> dict[str, Any]:
    """Get one project and its documentation, content, asset, and expense links."""
    return get_resource("projects", slug)


def list_assets(
    *, status: str | None = None, query: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List HQ assets, optionally filtered by exact status or text search."""
    return list_resource(
        "assets", {"status": status, "query": query, "limit": limit}
    )


def get_asset(slug: str) -> dict[str, Any]:
    """Get one asset and its project, documentation, content, and expense links."""
    return get_resource("assets", slug)


def list_managed_resources(*, limit: int = 50) -> dict[str, Any]:
    """List canonical desired and observed infrastructure state."""
    return list_resource("infrastructure.resources", {"limit": limit})


def get_managed_resource(key: str) -> dict[str, Any]:
    """Get one managed resource with structured operation evidence."""
    return get_resource("infrastructure.resources", key)


def list_services() -> dict[str, Any]:
    """List every declared hostname with the state of its DNS, ingress and TLS."""
    return list_resource("services")


def get_service(hostname: str) -> dict[str, Any]:
    """Get one hostname with the resources behind each part of its wiring."""
    return get_resource("services", hostname)


def list_expenses(
    *, year: int | None = None, category: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List expense records with stable relationship identifiers."""
    return list_resource(
        "expenses", {"year": year, "category": category, "limit": limit}
    )


def list_receipts(*, unmatched_only: bool = False, limit: int = 50) -> dict[str, Any]:
    """List receipt metadata only; never returns receipt file contents or URLs."""
    return list_resource(
        "receipts", {"unmatched_only": unmatched_only, "limit": limit}
    )


def documentation_status() -> dict[str, Any]:
    """Summarize AI-safe documentation pointers; sensitive records are excluded."""
    return read_models.documentation_status()


def recent_activity(*, limit: int = 25) -> dict[str, Any]:
    """Return recent HQ audit events without their free-form metadata payloads."""
    return read_models.recent_activity(limit=limit)


def system_health() -> dict[str, Any]:
    """Check database access and return non-sensitive record counts."""
    return read_models.system_health()


def dashboard_snapshot() -> dict[str, Any]:
    """Return HQ's canonical KPI, priority queue, and recent activity snapshot."""
    return operating_snapshot()
