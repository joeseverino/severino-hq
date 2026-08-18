"""Thin MCP adapters over HQ's canonical application services and safe queries."""

from __future__ import annotations

from typing import Any

from application import assets as asset_service
from application.dashboard import operating_snapshot
from application.capabilities import (
    describe_capabilities as describe_application_capabilities,
)
from application.capabilities import (
    execute_capability as execute_application_capability,
)
from application import projects as project_service
from application import infrastructure as infrastructure_service
from application import services as service_view
from application.security import mcp_principal
from application.registry import audit_registry as audit_application_registry
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
    return project_service.list_projects(status=status, query=query, limit=limit)


def get_project(slug: str) -> dict[str, Any]:
    """Get one project and its documentation, content, asset, and expense links."""
    try:
        return project_service.get_project(slug)
    except project_service.NotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


def list_assets(
    *, status: str | None = None, query: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List HQ assets, optionally filtered by exact status or text search."""
    return asset_service.list_assets(status=status, query=query, limit=limit)


def get_asset(slug: str) -> dict[str, Any]:
    """Get one asset and its project, documentation, content, and expense links."""
    try:
        return asset_service.get_asset(slug)
    except asset_service.NotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


def list_managed_resources(*, limit: int = 50) -> dict[str, Any]:
    """List canonical desired and observed infrastructure state."""
    return infrastructure_service.list_managed_resources(limit=limit)


def get_managed_resource(key: str) -> dict[str, Any]:
    """Get one managed resource with structured operation evidence."""
    try:
        return infrastructure_service.get_managed_resource(key)
    except infrastructure_service.NotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


def list_services() -> dict[str, Any]:
    """List every declared hostname with the state of its DNS, ingress and TLS."""
    return service_view.list_services()


def get_service(hostname: str) -> dict[str, Any]:
    """Get one hostname with the resources behind each part of its wiring."""
    try:
        return service_view.get_service(hostname)
    except infrastructure_service.NotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


def list_expenses(
    *, year: int | None = None, category: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List expense records with stable relationship identifiers."""
    return read_models.list_expenses(year=year, category=category, limit=limit)


def list_receipts(*, unmatched_only: bool = False, limit: int = 50) -> dict[str, Any]:
    """List receipt metadata only; never returns receipt file contents or URLs."""
    return read_models.list_receipts(unmatched_only=unmatched_only, limit=limit)


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
