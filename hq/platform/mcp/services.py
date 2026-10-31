"""Thin MCP adapters over HQ's canonical application services and safe queries."""

from typing import Any

from hq.platform.application import read_models
from hq.platform.application.capabilities import (
    describe_capabilities as describe_application_capabilities,
    execute_capability as execute_application_capability,
)
from hq.platform.application.connections import (
    describe_connections as describe_application_connections,
    list_connections as list_application_connections,
)
from hq.platform.application.dashboard import operating_snapshot
from hq.platform.application.findings import findings as application_findings
from hq.platform.application.registry import audit_registry as audit_application_registry
from hq.platform.application.reports import export_year_summary as export_application_year_summary
from hq.platform.application.resources import (
    describe_resources as describe_application_resources,
    get_resource as get_application_resource,
    list_resource as list_application_resource,
)
from hq.platform.application.security import Capability
from hq.platform.application.topology import topology as application_topology
from hq.platform.mcp.identity import current_principal


def describe_capabilities(name: str = "", resource: str = "") -> dict[str, Any]:
    """Describe JSON-executable HQ capabilities and their canonical schemas.

    The whole catalog is large: narrow it. ``name`` is a capability name or a
    prefix of whole dotted segments (``infrastructure`` returns every
    ``infrastructure.*``); ``resource`` keeps the capabilities that act on that
    resource, as `describe_resources` names it.
    """

    return describe_application_capabilities(principal=current_principal(), name=name, resource=resource)


def execute_capability(
    name: str,
    payload: dict[str, Any],
    target: str | int | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Execute one allowlisted capability from a schema-validated JSON payload.

    Some changes are held for a person. A result carrying
    `status: "awaiting_approval"` is not a failure and not something to retry:
    the request is recorded, nothing has been written, and an operator has to
    approve it in HQ's web interface, which this interface cannot do. Report the
    approval id from `approval.id` to whoever asked, and stop.

    Every capability whose effect is not `read` accepts an optional
    `idempotency_key` in its payload. Send one when a call may be retried: a
    repeat carrying the same key returns the first result instead of acting a
    second time. A `read` takes none.
    """

    return execute_application_capability(
        name,
        payload,
        principal=current_principal(),
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

    return list_application_connections(principal=current_principal())


def get_findings(rule: str = "", severity: str = "", limit: int | None = None) -> dict[str, Any]:
    """What HQ currently claims is wrong, with evidence and suggested remedies.

    Each remedy names an existing capability and target; run one with
    `execute_capability`. A remedy absent means this principal cannot run it.
    ``severity`` is a floor (``attention`` also returns ``serious``) and
    ``limit`` caps how many come back, most severe first; ``summary`` still
    counts them all and says whether any were held back.
    """

    return application_findings(
        principal=current_principal(), rule=rule.strip(), severity=severity.strip(), limit=limit
    )


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
        principal=current_principal(),
        lens=lens.strip(),
        focus=focus.strip(),
        direction=direction.strip(),
        depth=depth,
    )


def list_resource(name: str, filters: dict[str, Any] | None = None, fields: list[str] | None = None) -> dict[str, Any]:
    """List any registered resource with schema-validated filters.

    ``fields`` is a field mask read in each item: ``name`` keeps that whole value and
    ``runs.status`` keeps only ``status`` inside it, in every element of a list.
    A page is bounded by ``limit``: ``truncated`` says more matched than it holds,
    ``total`` how many where HQ counts them, and ``next`` how to ask for the rest.
    """

    return list_application_resource(name, filters, principal=current_principal(), strict=True, fields=fields)


def get_resource(name: str, identifier: str | int, fields: list[str] | None = None) -> dict[str, Any]:
    """Get one record from any registered addressable resource.

    ``fields`` is a field mask: ``observed_at`` keeps that whole value and
    ``items.waiting`` keeps only ``waiting`` in every element of ``items``.
    """

    return get_application_resource(name, identifier, principal=current_principal(), strict=True, fields=fields)


def _reader() -> None:
    """The caller may read HQ: what every read tool without a resource of its
    own asks, so no tool answers a token that holds nothing to read."""

    current_principal().require(Capability.READ)


def audit_registry() -> dict[str, Any]:
    """Report Project and Asset rows with no documentation references."""

    _reader()
    return audit_application_registry()


def export_year_summary(year: int, format: str = "md") -> dict[str, Any]:  # noqa: A002 - the MCP tool's parameter, by name
    """Export one safe year summary as Markdown or JSON."""

    return export_application_year_summary(year, format, principal=current_principal())


def documentation_status() -> dict[str, Any]:
    """Summarize AI-safe documentation pointers; sensitive records are excluded."""
    _reader()
    return read_models.documentation_status()


def recent_activity(*, limit: int = 25) -> dict[str, Any]:
    """Return recent HQ audit events without their free-form metadata payloads."""
    return read_models.recent_activity(principal=current_principal(), limit=limit)


def system_health() -> dict[str, Any]:
    """Check database access and return non-sensitive record counts."""
    _reader()
    return read_models.system_health()


def dashboard_snapshot() -> dict[str, Any]:
    """Return HQ's canonical KPI, priority queue, and recent activity snapshot."""
    return operating_snapshot(principal=current_principal())
