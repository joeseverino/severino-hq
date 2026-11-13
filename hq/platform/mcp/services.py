"""Thin MCP adapters over HQ's canonical application services and safe queries."""

from typing import Annotated, Any, cast

from pydantic import Field, SkipValidation

from hq.platform.application import read_models
from hq.platform.application.access import ANYONE, READS_A_LIST, READS_A_RECORD, RUNS_A_CAPABILITY, holding
from hq.platform.application.capabilities import (
    Performed,
    execute_capability as execute_application_capability,
)
from hq.platform.application.connections import (
    ConnectionCatalog,
    describe_connections as describe_application_connections,
)
from hq.platform.application.contracts import effect_glossary
from hq.platform.application.findings import FindingsQuery
from hq.platform.application.held_answer import HeldCall
from hq.platform.application.input_errors import unknown_field_errors
from hq.platform.application.integration_specs import FIELD_MASK, PAGE_FIELD
from hq.platform.application.projection import MAX_PAGE_SIZE
from hq.platform.application.read_models import DocumentationStatus, RecentActivity, SystemHealth
from hq.platform.application.registry import RegistryAudit, audit_registry as audit_application_registry
from hq.platform.application.reports import (
    ExportFormat,
    YearSummary,
    export_year_summary as export_application_year_summary,
)
from hq.platform.application.resources import (
    PAGING,
    Cursor,
    InvalidResourceInput,
    ResourceCatalog,
    ResourceCollection,
    describe_resources as describe_application_resources,
    get_resource as get_application_resource,
    list_resource as list_application_resource,
)
from hq.platform.application.security import Capability
from hq.platform.application.topology import TopologyQuery, topology as application_topology
from hq.platform.application.topology_answer import TopologyAnswer
from hq.platform.mcp.capability_answer import CapabilityDetail, CapabilityIndex, agent_capabilities
from hq.platform.mcp.dashboard_answer import ACTION_ITEMS_PAGE, AgentSnapshot, agent_snapshot
from hq.platform.mcp.finding_actions import AgentFindings, agent_findings
from hq.platform.mcp.identity import current_principal
from hq.platform.mcp.shapes import Budget, tool

# The kind whose first page this tool answers smaller than the kind's own default.
QUEUE = "action_items"

# A page's size, beside the filters; the kind's own query holds it to its bounds.
type PageSize = Annotated[
    int | None,
    Field(
        description=(
            f"How many records a page holds: at most {MAX_PAGE_SIZE}, or the lower cap `describe_resources` "
            f"names for a kind. Without it, the kind's default, or {ACTION_ITEMS_PAGE} for `{QUEUE}`."
        )
    ),
]


@tool("Capability catalog", requires=ANYONE, budget=Budget(15_800))
def describe_capabilities(name: str = "", resource: str = "") -> CapabilityIndex | CapabilityDetail:
    """The capabilities `execute_capability` runs. With no argument, an
    index without schemas. Narrowed by `name` or `resource`, each capability
    in full with its `input_schema`, whose `#/$defs/<name>` refer to the
    answer's root `$defs`.

    `name` is a capability's name or a prefix of whole dotted segments:
    `infrastructure` returns every `infrastructure.*`. `resource` keeps the
    capabilities that act on that resource, as `describe_resources` names it.
    """

    return agent_capabilities(principal=current_principal(), name=name, resource=resource)


# The meaning of each effect is declared once, beside the effects themselves.
describe_capabilities.__doc__ = f"{describe_capabilities.__doc__}\n{effect_glossary('effect')}\n"


@tool(
    "Run a capability",
    requires=RUNS_A_CAPABILITY,
    budget=Budget(
        420,
        frozendict(
            {
                "name": "project.create",
                "payload": frozendict(
                    {"name": "Budget", "slug": "budget", "status": "active", "idempotency_key": "example-budget"}
                ),
            }
        ),
    ),
)
def execute_capability(
    name: str,
    # Absent means an empty one, as an answer's left-out field means its default.
    payload: dict[str, Any] | None = None,
    # Read by the capability runner once the caller may run it, as every door's are.
    target: SkipValidation[str | int | None] = None,
    expected_updated_at: SkipValidation[str | None] = None,
) -> Performed | HeldCall:
    """Run one capability with a JSON payload its schema checks.

    A result with `status: "awaiting_approval"` is not a failure, and not
    one to retry. Nothing was written, and an operator approves or rejects
    it in HQ's web interface. Report `approval.id` to the user you act for,
    and stop.

    Every capability whose effect is not `read` requires an
    `idempotency_key` in its payload, new for each change. A retry with the
    same key returns the first result instead of acting twice.
    """

    answer = execute_application_capability(
        name,
        payload or {},
        principal=current_principal(),
        target=target,
        expected_updated_at=expected_updated_at,
    )
    # A capability's own result, or one held for a person; a refusal (``ok``
    # false) is answered as the refusal every tool's shape lists beside it.
    return cast(Performed | HeldCall, answer)


@tool("Resource catalog", requires=ANYONE, budget=Budget(12_800))
def describe_resources(name: str | None = None) -> ResourceCatalog:
    """Every resource HQ can read, the operations each supports, and whether you may use each.

    Each list names the `filters` it takes. Pass a kind as `name` to get its
    `query_schema`, with each filter's type and values, and its `paging`:
    the largest `limit`, and `cursor` where the list pages.
    """

    return describe_application_resources(principal=current_principal(), name=name or "")


@tool("Connection catalog", requires=ANYONE, budget=Budget(16_000))
def describe_connections() -> ConnectionCatalog:
    """The installed connection families, with their abilities, scopes and routes, and whether you may use each."""

    return describe_application_connections(principal=current_principal())


@tool("Open findings", requires=holding(Capability.READ), arguments=FindingsQuery, budget=Budget(5_000))
def get_findings(**narrowing: Any) -> AgentFindings:
    """The open problems HQ found, most severe first, each with its evidence and remedies.

    `system_health` says only whether HQ answers. The `status-brief` prompt
    returns health and problems together.

    Every action a finding carries (a remedy, an offer, an investigation, a
    step of its plan) is a tool call: send its `arguments` to its `tool`. A
    finding lists only the remedies you may run.

    `severity` is a floor: `attention` also returns `serious`. `limit` caps
    how many come back, and without it only the few most severe do.
    `summary` counts them all and says whether any were left out; a higher
    `limit` or a `rule` reads those.
    """

    return agent_findings(principal=current_principal(), **narrowing)


@tool("Infrastructure topology", requires=holding(Capability.READ), arguments=TopologyQuery, budget=Budget(1_300))
def get_topology(**narrowing: Any) -> TopologyAnswer:
    """The live infrastructure graph, each node with the safe actions it offers.

    With no lens and no focus, it returns the counts by kind and the lenses
    you can pass. A lens answers one standing question. A node id as `focus`
    traces `inbound`, `outbound` or `both`, up to five hops. An unknown
    focus or depth is refused with the valid choices.
    """

    return application_topology(principal=current_principal(), **narrowing)


@tool(
    "List records",
    requires=READS_A_LIST,
    budget=Budget(1_700, frozendict({"name": "action_items"})),
)
def list_resource(
    name: str,
    filters: dict[str, Any] | None = None,
    fields: Annotated[list[str] | None, Field(description=FIELD_MASK)] = None,
    limit: PageSize = None,
    cursor: Cursor = None,
) -> ResourceCollection:
    """One page of records of a kind, narrowed by the filters its schema checks.

    `name` is one of the kinds the schema lists. `filters` are the ones that
    kind takes, named for each kind below. `describe_resources` says what
    each kind holds, with each filter's type and choices.

    Each item holds the fields `describe_resources` lists for its kind, or
    every field where it lists none. `fields` picks others.

    `limit` bounds the page. `truncated` says more follow, and `total` says
    how many matched, where HQ counts them. Send `next.cursor` back as
    `cursor`, with the same filters, for the next page. `next` is null on
    the last page, and on a list that does not page.
    """

    query = dict(filters or {})
    if paging := sorted(PAGING & query.keys()):
        # Paging is an argument of its own, as on every tool that pages.
        raise InvalidResourceInput(name, unknown_field_errors([f"filters.{field}" for field in paging]))
    # The queue's page is this tool's own; the kind's query still bounds a limit sent.
    if limit is None and name == QUEUE:
        limit = ACTION_ITEMS_PAGE
    query |= {field: value for field, value in (("limit", limit), (PAGE_FIELD, cursor)) if value is not None}
    return list_application_resource(name, query, principal=current_principal(), strict=True, fields=fields)


# One record of its kind, with the fields describe_resources names for it.
@tool(
    "Read one record", requires=READS_A_RECORD, budget=Budget(700, frozendict({"name": "projects", "identifier": "hq"}))
)
def get_resource(
    name: str, identifier: str | int, fields: Annotated[list[str] | None, Field(description=FIELD_MASK)] = None
) -> dict[str, Any]:
    """One record of a kind, by its identifier.

    `name` is one of the kinds the schema lists, and `describe_resources`
    says what each holds. `identifier` is the record's value of the field
    its kind is found by, listed below.
    """

    return get_application_resource(name, identifier, principal=current_principal(), strict=True, fields=fields)


@tool("Undocumented projects and assets", requires=holding(Capability.READ), budget=Budget(120))
def audit_registry() -> RegistryAudit:
    """The projects and assets that no doc covers yet.

    `documentation_status` covers the docs that exist.
    """

    return audit_application_registry(principal=current_principal())


@tool("Export a year summary", requires=holding(Capability.READ), budget=Budget(4_700, frozendict({"year": 2026})))
def export_year_summary(year: int, format: ExportFormat = "md") -> YearSummary:  # noqa: A002 - the MCP tool's parameter, by name
    """One year's summary, as Markdown or JSON: its expenses, projects,
    writeups, docs and the assets bought.

    It suits a report to keep or file, and each export is recorded in the
    audit log. `dashboard_snapshot` reads HQ's live state.
    """

    return export_application_year_summary(year, format, principal=current_principal())


@tool("Documentation coverage", requires=holding(Capability.READ), budget=Budget(1_200))
def documentation_status() -> DocumentationStatus:
    """The docs HQ knows of: counts by status and type, and the first page
    of docs, each with its title, system and location. Sensitive docs are
    left out.

    For the next page, send `next.cursor` as the `cursor` of
    `list_resource` with the name `documentation`. `next` is null when this
    page holds them all. `audit_registry` names the records no doc covers.
    """
    return read_models.documentation_status(principal=current_principal())


@tool("What changed lately", requires=holding(Capability.READ_AUDIT_LOG), budget=Budget(12_500))
def recent_activity(*, limit: int = 25) -> RecentActivity:
    """The newest audit-log events, newest first: who did what to which
    record, and when.

    It needs the `read_audit_log` permission. `dashboard_snapshot` reads
    what needs doing.
    """
    return read_models.recent_activity(principal=current_principal(), limit=limit)


@tool("HQ health check", requires=holding(Capability.READ), budget=Budget(200))
def system_health() -> SystemHealth:
    """Whether HQ and its database answer, with the record count of each domain.

    `get_findings` reads what is wrong, and `dashboard_snapshot` what needs
    doing. The `status-brief` prompt returns health and open problems
    together.
    """
    return read_models.system_health(principal=current_principal())


@tool("HQ at a glance", requires=holding(Capability.READ), budget=Budget(7_600))
def dashboard_snapshot() -> AgentSnapshot:
    """What needs doing in HQ: its figures, the most urgent items in the queue and the docs due a review.

    Call it first for what needs doing. `system_health` says whether HQ
    answers, and `get_findings` what is wrong. The `status-brief` prompt
    returns both together.

    `priority` is the queue's first page, and `priority_count` counts the
    whole queue. Each queue item's `steps` are the plan that resolves it,
    in order. `lists` names the call for each of HQ's other lists (the
    whole queue, recent activity, active projects, draft and published
    content): send its `arguments` to its `tool`. `unread` names each part
    you may not read.
    """

    return agent_snapshot(principal=current_principal())
