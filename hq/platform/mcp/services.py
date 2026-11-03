"""Thin MCP adapters over HQ's canonical application services and safe queries."""

from typing import Annotated, Any, cast

from pydantic import Field, SkipValidation

from hq.platform.application import read_models
from hq.platform.application.capabilities import (
    CapabilityCatalog,
    Performed,
    describe_capabilities as describe_application_capabilities,
    execute_capability as execute_application_capability,
)
from hq.platform.application.connections import (
    ConnectionCatalog,
    describe_connections as describe_application_connections,
)
from hq.platform.application.contracts import effect_glossary
from hq.platform.application.dashboard import OperatingSnapshot, operating_snapshot
from hq.platform.application.findings import (
    RULES,
    SEVERITIES,
    Findings,
    findings as application_findings,
)
from hq.platform.application.held_answer import HeldCall
from hq.platform.application.integration_specs import FIELD_MASK
from hq.platform.application.read_models import DocumentationStatus, RecentActivity, SystemHealth
from hq.platform.application.registry import RegistryAudit, audit_registry as audit_application_registry
from hq.platform.application.reports import (
    ExportFormat,
    YearSummary,
    export_year_summary as export_application_year_summary,
)
from hq.platform.application.resources import (
    ResourceCatalog,
    ResourceCollection,
    describe_resources as describe_application_resources,
    get_resource as get_application_resource,
    list_resource as list_application_resource,
)
from hq.platform.application.topology_answer import TopologyAnswer
from hq.platform.mcp.identity import current_principal
from hq.platform.mcp.shapes import tool
lazy from hq.platform.application.topology import topology as application_topology

# Where the lenses and directions are declared, apart from the graph that
# derives them, so the tool list names them without loading the graph.
lazy from hq.platform.application.topology_lenses import TOPOLOGY_LENSES, TRACE_DIRECTIONS


@tool("Capability catalog")
def describe_capabilities(name: str = "", resource: str = "") -> CapabilityCatalog:
    """Describe JSON-executable HQ capabilities and their canonical schemas.

    The whole catalog is large: narrow it. `name` is a capability name or a
    prefix of whole dotted segments (`infrastructure` returns every
    `infrastructure.*`); `resource` keeps the capabilities that act on that
    resource, as `describe_resources` names it.
    """

    return describe_application_capabilities(principal=current_principal(), name=name, resource=resource)


# The meaning of each effect is declared once, beside the effects themselves.
describe_capabilities.__doc__ = f"{describe_capabilities.__doc__}\n{effect_glossary('effect')}\n"


@tool("Run a capability")
def execute_capability(
    name: str,
    payload: dict[str, Any],
    # Read by the capability runner once the caller may run it, as every door's are.
    target: SkipValidation[str | int | None] = None,
    expected_updated_at: SkipValidation[str | None] = None,
) -> Performed | HeldCall:
    """Execute one allowlisted capability from a schema-validated JSON payload.

    Some changes are held for a person. A result carrying
    `status: "awaiting_approval"` is not a failure and not something to retry:
    the request is recorded, nothing has been written, and an operator has to
    approve it in HQ's web interface, which this interface cannot do. Report the
    approval id from `approval.id` to whoever asked, and stop.

    Every capability whose effect is not `read` requires an `idempotency_key`
    in its payload, new for each change: a repeat carrying the same key
    returns the first result instead of acting a second time. A `read` takes
    none.
    """

    answer = execute_application_capability(
        name,
        payload,
        principal=current_principal(),
        target=target,
        expected_updated_at=expected_updated_at,
    )
    # A capability's own result, or one held for a person; a refusal (``ok``
    # false) is answered as the refusal every tool's shape lists beside it.
    return cast(Performed | HeldCall, answer)


@tool("Resource catalog")
def describe_resources(name: str | None = None) -> ResourceCatalog:
    """Describe every readable HQ resource, its supported operations, and whether you are permitted each.

    Each list names the `filters` it narrows by. Pass a kind as `name` for
    that one resource with its list's whole `query_schema`: each filter's
    type, the values it takes, and the most `limit` may ask for.
    """

    return describe_application_resources(principal=current_principal(), name=name or "")


@tool("Connection catalog")
def describe_connections() -> ConnectionCatalog:
    """Describe installed connection families, abilities, scopes, routes, and whether you are permitted each."""

    return describe_application_connections(principal=current_principal())


@tool(
    "Open findings",
    choices={"rule": lambda: (rule.name for rule in RULES), "severity": lambda: SEVERITIES},
)
def get_findings(rule: str | None = None, severity: str | None = None, limit: int | None = None) -> Findings:
    """What HQ currently claims is wrong, with evidence and suggested remedies.

    Pick it for the open problems themselves; `system_health` says only whether
    HQ answers, and the `status-brief` prompt puts both before a person.

    Each remedy names an existing capability and target; run one with
    `execute_capability`. A remedy absent means this principal cannot run it.
    `severity` is a floor (`attention` also returns `serious`) and
    `limit` caps how many come back, most severe first; `summary` still
    counts them all and says whether any were held back.
    """

    return application_findings(principal=current_principal(), rule=rule or "", severity=severity or "", limit=limit)


@tool(
    "Infrastructure topology",
    choices={"lens": lambda: (lens.name for lens in TOPOLOGY_LENSES), "direction": lambda: TRACE_DIRECTIONS},
)
def get_topology(
    lens: str | None = None,
    focus: str = "",
    direction: str = "both",
    depth: int = 2,
) -> TopologyAnswer:
    """Return the live infrastructure graph with safe canonical actions.

    With no lens and no focus it answers the counts by kind and the lenses
    that narrow it; pass a lens name for a standing question, or a node id as
    `focus` to trace `inbound`, `outbound` or `both` up to five hops. An
    unknown focus or depth is refused with the valid choices.
    Every node retains its canonical safe actions.
    """

    return application_topology(
        principal=current_principal(),
        lens=lens or "",
        focus=focus.strip(),
        direction=direction or "both",
        depth=depth,
    )


@tool("List records")
def list_resource(
    name: str,
    filters: dict[str, Any] | None = None,
    fields: Annotated[list[str] | None, Field(description=FIELD_MASK)] = None,
) -> ResourceCollection:
    """List any registered resource with schema-validated filters.

    `name` is one of the kinds the schema lists; `describe_resources` says
    what each holds and the `filters` it takes.

    Each item holds the fields its kind lists, as `describe_resources`
    names them, or every field where it names none; `fields` says which
    instead.

    A page is bounded by `limit`: `truncated` says more follow it, `total`
    how many matched where HQ counts them, and `next.cursor`, sent back as the
    `cursor` filter beside the same filters, reads the page after; `next` is
    null on the last page and on a list that does not page.
    """

    return list_application_resource(name, filters, principal=current_principal(), strict=True, fields=fields)


# One record of its kind, with the fields describe_resources names for it.
@tool("Read one record")
def get_resource(
    name: str, identifier: str | int, fields: Annotated[list[str] | None, Field(description=FIELD_MASK)] = None
) -> dict[str, Any]:
    """Get one record from any registered addressable resource.

    `name` is one of the kinds the schema lists, and `describe_resources`
    says what each holds; `identifier` is the record's value of the field
    its kind is addressed by, listed below.
    """

    return get_application_resource(name, identifier, principal=current_principal(), strict=True, fields=fields)


@tool("Undocumented projects and assets")
def audit_registry() -> RegistryAudit:
    """The Project and Asset records no documentation points to.

    Pick it to find what still needs a doc written; `documentation_status`
    says what the docs that do exist cover.
    """

    return audit_application_registry(principal=current_principal())


@tool("Export a year summary")
def export_year_summary(year: int, format: ExportFormat = "md") -> YearSummary:  # noqa: A002 - the MCP tool's parameter, by name
    """One year's summary as text: its expenses, projects, writeups,
    documentation pointers and assets bought, as Markdown or JSON.

    Pick it for a report to keep or file. Each export is recorded in the
    audit log. For the live state of HQ, `dashboard_snapshot` is the read.
    """

    return export_application_year_summary(year, format, principal=current_principal())


@tool("Documentation coverage")
def documentation_status() -> DocumentationStatus:
    """The documentation HQ points to: counts by status and type, and the
    first page of records, each with its title, system and where it lives.
    Sensitive records are left out.

    `next.cursor`, sent as the `cursor` filter of `list_resource` with name
    `documentation`, reads the page after; `next` is null when this page holds
    them all. Pick it to find where a doc lives or which need review;
    `audit_registry` names the records with no doc at all.
    """
    return read_models.documentation_status(principal=current_principal())


@tool("What changed lately")
def recent_activity(*, limit: int = 25) -> RecentActivity:
    """The newest audit-log events, newest first: who did what to which
    record, and when, without the events' metadata.

    Needs the audit-log read permission (`read_audit_log`); a caller without it
    is refused. Pick it for what changed lately; `dashboard_snapshot` carries
    the same events beside HQ's other figures.
    """
    return read_models.recent_activity(principal=current_principal(), limit=limit)


@tool("HQ health check")
def system_health() -> SystemHealth:
    """Whether HQ answers: its database is checked, and the record count of each domain returned.

    Pick it first to see that HQ is up. `get_findings` holds what is wrong,
    `dashboard_snapshot` what needs doing, and the `status-brief` prompt is
    health and open problems together for a person.
    """
    return read_models.system_health(principal=current_principal())


@tool("HQ at a glance")
def dashboard_snapshot() -> OperatingSnapshot:
    """HQ at a glance: its figures, the priority queue, work in progress and recent activity.

    Pick it first for what needs doing. `system_health` says whether HQ
    answers, `get_findings` what is wrong, and the `status-brief` prompt is
    both for a person.

    A part this caller may not read is left empty and named in `unread`; an
    empty `recent_activity` with `unread` empty means there was no activity.
    """

    return operating_snapshot(principal=current_principal())
