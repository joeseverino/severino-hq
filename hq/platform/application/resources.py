"""Discoverable, authorized read resources shared by every HQ adapter."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal, NotRequired, TypedDict, override

from django.core import signing
from django.db.models import Model
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    create_model,
    field_validator,
    with_config,
)

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS, spec_withheld
from hq.platform.core.models import AuditLog

from . import (
    connection_context,
    container_reads,
    derived_reads,
    hq_context,
    infrastructure,
    resource_context,
    service_list,
    services,
    tailnet_context,
)
from .answer_fields import Permitted, Pinned, RequiredPermissions, SchemaVersion, WebRoute
from .connection_records import (
    ConnectionStandingRecord,
    ConnectionStandingRow,
    CredentialRecord,
    CredentialRow,
    Tailnet,
)
from .contracts import DOTTED_NAME
from .describe import Describe
from .field_masks import FieldMaskRefusal, Fields, asks_whole, selected
from .input_errors import pydantic_refusal
from .integration_specs import PAGE_FIELD, ResourceSpec
from .integration_validation import required_capability_names
from .integrations import integration_graph
from .json_schemas import json_schema
from .projection import MAX_PAGE_SIZE, CursorRefusal, NextPage, PageScope, page_size, paging
from .record_errors import HostRefusal, RecordNotFound
from .refusal_status import next_call
from .resource_records import (
    ActionItemRow,
    ContainerRecord,
    ContainerRow,
    DomainRecord,
    DomainRow,
    EstateFigure,
    HopRow,
    MachineRecord,
    MachineRow,
    ManagedResourceRecord,
    ManagedResourceRow,
    Match,
    PathRecord,
    Problem,
    ReadingRecord,
    ReadingRow,
    RelationshipsRecord,
    ServiceRecord,
    ServiceRow,
    UpgradeRecord,
    UpgradeRow,
)
from .search_contracts import SearchDefinition, search_lines
from .security import Capability, Principal, require_all
lazy from .domains import all_domains
lazy from .plugins import ATTENTION_ORDER


class ResourceSearchDefinition(SearchDefinition):
    """A declaration's result opens the machine, service or domain it belongs to."""

    @override
    def url(self, instance: Any) -> str:
        return services.home_url(instance)

    @override
    def body(self, instance: Model) -> str:
        """The indexed text; a withheld kind's spec is left out (``ProviderSpec.withheld``)."""

        # Each name is a field the definition declares on its model.
        withheld = spec_withheld(str(instance.serializable_value("kind")))
        fields = tuple(field for field in self.fields if field != "spec" or not withheld)
        return "\n".join(line for field in fields for line in search_lines(instance.serializable_value(field)))


class ResourceQuery(BaseModel):
    """Base for list inputs: unknown fields are always programmer errors."""

    model_config = ConfigDict(extra="forbid")


def limit_words(most: int, *, paged: bool) -> str:
    """What a list query's ``limit`` says, its bound from the value it is checked by."""

    return (
        f"How many records a page holds, 1 to {most}; next.cursor reads the page after."
        if paged
        else f"How many records to return, 1 to {most}, first by the list's own ranking."
    )


# Each list's ``limit`` is a named alias, so the API document writes it once
# for every query that takes it; a query whose default differs takes an alias
# of its own, since the parameter the document writes carries the default.
# The fields every paged list takes:
type PageLimit = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description=limit_words(MAX_PAGE_SIZE, paged=True))]
type Cursor = Annotated[
    str | None,
    Field(
        max_length=4000,
        description=(
            "The page after the one whose next.cursor this is. Send it as it came, with the filters that page was "
            "read with; absent, the first page."
        ),
    ),
]


# What a list's own ``status`` and ``query`` narrow by, wherever a query declares them.
STATUS_FILTER = "Only the records in this status; absent, every status."
TEXT_FILTER = "Only the records whose text holds this, ignoring case; absent, every record."
# A list's ``query`` where it may be left out.
type TextFilter = Annotated[str | None, Field(description=TEXT_FILTER)]
# A list's ``status`` where it may be left out and its statuses are not
# declared as a choice; one that declares them types the field by ``one_of``.
type StatusFilter = Annotated[str | None, Field(description=STATUS_FILTER)]
# A ranked list's ``limit``: the deployment-wide ceiling, declared so a caller
# reads it in the schema and a page past it is refused rather than quietly cut.
type TopLimit = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description=limit_words(MAX_PAGE_SIZE, paged=False))]
# Search's, which answers fewer unless asked for more.
type SearchLimit = Annotated[
    int, Field(ge=1, le=MAX_PAGE_SIZE, description=f"How many matches to return, 1 to {MAX_PAGE_SIZE}, best first.")
]


# The fields that page a list rather than narrow it: MCP's list_resource
# takes them beside its filters, and a listing names its filters without them.
PAGING = frozenset({"limit", PAGE_FIELD})


def list_filters(query: type[BaseModel]) -> tuple[str, ...]:
    """What a list query narrows by: every field but the ones that page it."""

    return tuple(name for name in query.model_fields if name not in PAGING)


class TopQuery(ResourceQuery):
    """A list's first ``limit`` by its own ranking, which a read narrows rather than pages past."""

    limit: TopLimit = 50


class BoundedQuery(TopQuery):
    """A list read one page at a time, every row reached by following each page's ``next``."""

    limit: PageLimit = 50
    cursor: Cursor = None


def one_of(values: Iterable[str]) -> Any:
    """A ``Literal`` of ``values``, taken from where they are declared: a query
    field typed by it lists them as its schema's enum and refuses any other,
    naming the field.

    Built at run time, so a query model declares such a field through
    ``create_model``: a type checker reads a ``Literal`` only from literals.
    """

    chosen = tuple(dict.fromkeys(values))
    if not chosen:
        raise ValueError("A choice needs at least one value to choose.")
    return Literal[chosen]


class _KindsQuery(BoundedQuery):
    kinds: str | None = Field(
        default=None,
        max_length=2000,
        description="Several types to keep, comma-separated, each one the single type filter offers; not sent with it.",
    )

    @field_validator("kinds")
    @classmethod
    def valid_kinds(cls, value: str | None) -> str | None:
        if value is None:
            return None
        kinds = value.split(",")
        if not kinds or any(not DOTTED_NAME.fullmatch(kind) for kind in kinds):
            raise ValueError("kinds must be comma-separated dotted resource kinds")
        if len(kinds) != len(set(kinds)):
            raise ValueError("kinds must not repeat a resource kind")
        if any(kind not in PROVIDERS for kind in kinds):
            raise ValueError("kinds must each be a resource kind the kind field lists")
        return value


InfrastructureResourceQuery = create_model(
    "InfrastructureResourceQuery",
    __base__=_KindsQuery,
    kind=(
        one_of(PROVIDERS) | None,
        Field(default=None, description="Only the records of this type; not sent with the filter of several."),
    ),
)


class EmptyQuery(ResourceQuery):
    pass


def action_item_query() -> type[BoundedQuery]:
    """The queue's narrowing: a status the queue holds, and a domain that raises
    items, as this composition declares them."""

    sources = (domain.id for domain in all_domains() if domain.integration.attention is not None)
    return create_model(
        "ActionItemQuery",
        __base__=BoundedQuery,
        query=(str, Field(default="", max_length=200, description=TEXT_FILTER)),
        status=(
            one_of(ATTENTION_ORDER) | None,
            Field(default=None, description="Only the items this urgent; absent, every item."),
        ),
        source=(
            one_of(sources) | None,
            Field(default=None, description="Only the items from this section; absent, every section's."),
        ),
    )


ReadingQuery = create_model(
    "ReadingQuery",
    __base__=BoundedQuery,
    provider=(
        one_of(sorted({spec.provider for spec in OBSERVATIONS.values()})) | None,
        Field(default=None, description="Only the readings from this service; absent, every service's."),
    ),
)


# Search ranks each scope apart and cuts each before merging them, so no one
# order holds a place to resume from: a caller narrows the query instead.
class SearchQuery(TopQuery):
    query: str = Field(
        default="",
        max_length=200,
        description="What to look for in names and records' text; blank finds nothing.",
    )
    limit: SearchLimit = 20


def core_resource_specs() -> tuple[ResourceSpec, ...]:
    """The host's own resources, built when the registry is composed, since the
    queue's sources are the composed domains."""

    return (
        ResourceSpec(
            "audit",
            "Audit log",
            "Security-sensitive audit events indexed by HQ.",
            Capability.READ_AUDIT_LOG,
            search=SearchDefinition(
                "audit",
                AuditLog,
                "pk",
                (
                    "action",
                    "object_type",
                    "object_id",
                    "object_repr",
                    "operation_id",
                    "message",
                ),
                label="Audit log",
                title_field="search_title",
                badge_field="type_label",
                timestamp_field="created_at",
                snippet_field="search_snippet",
            ),
            web_route="core:audit_list",
        ),
        ResourceSpec(
            "infrastructure",
            "Records",
            "Everything HQ manages: what HQ expects and what is live.",
            Capability.READ,
            infrastructure.list_managed_resources,
            InfrastructureResourceQuery,
            resource_context.get_managed_resource,
            "key",
            search=ResourceSearchDefinition(
                "infrastructure",
                ManagedResource,
                "key",
                ("key", "kind", "spec", "status", "conditions"),
                label="Records",
                title_field="key",
                badge_field="kind_label",
                snippet_field="search_summary",
            ),
            web_route="control_plane:list",
            row_type=ManagedResourceRow,
            record_type=ManagedResourceRecord,
        ),
        ResourceSpec(
            "services",
            "Services",
            "Every service name and the state of its DNS, proxy and certificate.",
            Capability.READ,
            service_list.list_services,
            EmptyQuery,
            service_list.get_service,
            "hostname",
            web_route="control_plane:services",
            row_type=ServiceRow,
            record_type=ServiceRecord,
        ),
        ResourceSpec(
            "estate",
            "Estate",
            "Machines online, services, domains, next renewals and connections needing attention.",
            Capability.READ,
            derived_reads.list_estate,
            EmptyQuery,
            web_route="dashboard",
            pass_principal=True,
            row_type=EstateFigure,
        ),
        ResourceSpec(
            "machines",
            "Machines",
            "Every machine anything reported, its roles, addresses and what runs on it.",
            Capability.READ,
            derived_reads.list_machines,
            BoundedQuery,
            derived_reads.get_machine,
            "name",
            web_route="control_plane:machines",
            row_type=MachineRow,
            record_type=MachineRecord,
        ),
        ResourceSpec(
            "containers",
            "Containers",
            "Every running container: what it runs and whether that is current and safe, "
            "how it is run and the memory and CPU it uses beside its limits, and its posture against the "
            "container standard.",
            Capability.READ,
            container_reads.list_containers,
            BoundedQuery,
            container_reads.get_container,
            "address",
            web_route="control_plane:containers",
            row_type=ContainerRow,
            record_type=ContainerRecord,
        ),
        ResourceSpec(
            "upgrades",
            "Upgrades",
            "Every running container something newer is published for: the target by digest, "
            "what it fixes, its data and how it would be verified, every reason it cannot go "
            "ahead yet, and the steps an upgrade would take. Read-only.",
            Capability.READ,
            container_reads.list_upgrades,
            BoundedQuery,
            container_reads.get_upgrade,
            "address",
            web_route="control_plane:containers",
            row_type=UpgradeRow,
            record_type=UpgradeRecord,
        ),
        ResourceSpec(
            "domains",
            "Domains",
            "Every domain HQ manages or has read, its services and registration.",
            Capability.READ,
            derived_reads.list_domains,
            BoundedQuery,
            derived_reads.get_domain,
            "name",
            web_route="zones:index",
            row_type=DomainRow,
            record_type=DomainRecord,
        ),
        ResourceSpec(
            "relationships",
            "Relationships",
            "One machine, service, domain or record and everything related to it.",
            Capability.READ,
            detail_handler=derived_reads.get_relationships,
            identifier="node",
            web_route="control_plane:topology",
            pass_principal=True,
            record_type=RelationshipsRecord,
        ),
        ResourceSpec(
            "service.paths",
            "Service paths",
            "The path a request for a service's hostname takes, hop by hop, with each hop's reading and certificate.",
            Capability.READ,
            detail_handler=derived_reads.get_path,
            identifier="hostname",
            web_route="control_plane:services",
            record_type=PathRecord,
        ),
        ResourceSpec(
            "hq",
            "HQ",
            "HQ itself: the release it runs and whether its branch is ahead, the machine and "
            "container it runs in, its background jobs, what it can read and its open problems.",
            Capability.READ,
            hq_context.read_hq,
            EmptyQuery,
            web_route="hq_self",
            pass_principal=True,
            row_type=Problem,
        ),
        ResourceSpec(
            "readings",
            "Readings",
            "Each type of reading, when it was last read, and the records it holds.",
            Capability.READ,
            derived_reads.list_readings,
            ReadingQuery,
            derived_reads.get_reading,
            "kind",
            web_route="control_plane:connections",
            row_type=ReadingRow,
            record_type=ReadingRecord,
        ),
        ResourceSpec(
            "credentials",
            "Credential access",
            "What each connection's credential can read, and what it would need to read more.",
            Capability.READ,
            derived_reads.list_credentials,
            EmptyQuery,
            derived_reads.get_credential,
            "provider",
            web_route="control_plane:connections",
            row_type=CredentialRow,
            record_type=CredentialRecord,
        ),
        ResourceSpec(
            "tailnet",
            "Tailnet",
            "The tailnet policy: settings, grants, shell rules, groups and tags with their "
            "machines, services, app connectors, tests, problems, and what could not be read.",
            Capability.READ,
            tailnet_context.get_tailnet,
            EmptyQuery,
            web_route="control_plane:tailnet",
            pass_principal=True,
            row_type=Tailnet,
        ),
        ResourceSpec(
            "connections",
            "Connection status",
            "Each connection with what its credential sees, how fresh each reading is, what "
            "was refused and the fix; the summary counts, the security posture and HQ's path.",
            Capability.READ,
            connection_context.list_connection_standing,
            EmptyQuery,
            connection_context.get_connection_standing,
            "connection_ref",
            web_route="control_plane:connections",
            pass_principal=True,
            row_type=ConnectionStandingRow,
            record_type=ConnectionStandingRecord,
        ),
    )


# A host domain's own resources, kept here beside the rest of the host's: each
# domain's declaration names its function (``domains.HOST_DOMAINS``).


def dashboard() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "action_items",
            "Needs you",
            "The composed action queue: what needs doing, from every domain.",
            Capability.READ,
            derived_reads.list_action_items,
            action_item_query(),
            web_route="action_items",
            pass_principal=True,
            row_type=ActionItemRow,
        ),
    )


def connections() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "request.path",
            "Request path",
            "How the calling request reached HQ: HQ's own path hop by hop, each hop "
            "checked against what the request shows, with the admission layers decided there.",
            Capability.READ,
            derived_reads.list_request_path,
            EmptyQuery,
            web_route="connection",
            row_type=HopRow,
        ),
    )


def search() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "search",
            "Search",
            "Machines, services and domains, then records, matching a query.",
            Capability.READ,
            derived_reads.search,
            SearchQuery,
            web_route="search",
            pass_principal=True,
            row_type=Match,
        ),
    )


class ResourceError(ValueError):
    """Base for resource failures: ``reason`` is this module's own text.

    Adapters answer a caller with ``reason``, never ``str(exc)``: a relayed
    handler message can name internals the caller has no business seeing.
    """

    def __init__(self, reason: str = "", *args: object) -> None:
        super().__init__(reason, *args)
        self.reason = reason


class UnknownResource(HostRefusal, ResourceError):
    code = "unknown_resource"


class UnsupportedResourceOperation(HostRefusal, ResourceError):
    code = "unsupported_operation"


class InvalidResourceInput(ResourceError):
    """Input a resource's read refused, said once for every door: each field
    that was wrong and why, the filters its kind's list takes, and the call
    that says each one's type, so the next call can be right."""

    def __init__(self, name: str, errors: Iterable[Mapping[str, Any]]) -> None:
        refusal = pydantic_refusal(name, errors)
        spec = resource_registry().get(name)
        details: dict[str, Any] = {"errors": refusal.details, "next": next_call("describe_resources", name=name)}
        if spec is not None and spec.list_query_type:
            details["filters"] = list(list_filters(spec.list_query_type))
        super().__init__(refusal.message)
        self.details = details


# What a missed identifier adds to its refusal's sentence.
FOUND_BY = (
    " details.identifier names the field this resource's records are addressed by, and details.next "
    "the listing that finds a record's value."
)


def _found_by(spec: ResourceSpec) -> dict[str, Any]:
    """What finds a record no identifier matched: the field its resource is
    addressed by, and the listing that holds each record's value."""

    details: dict[str, Any] = {"identifier": spec.identifier}
    if spec.list_query_type:
        details["next"] = next_call("list_resource", name=spec.name)
    return details


def _served(spec: ResourceSpec) -> dict[str, Any]:
    """The read a resource does serve, for a refusal of one it does not: its
    listing, or one record by the field it is addressed by, or else the catalog."""

    if spec.list_handler and spec.list_query_type:
        return {"next": next_call("list_resource", name=spec.name)}
    if spec.detail_handler and spec.identifier:
        return {"identifier": spec.identifier, "next": next_call("get_resource", name=spec.name)}
    return {"next": next_call("describe_resources")}


def resource_registry() -> dict[str, ResourceSpec]:
    return dict(integration_graph().resources)


def resource_search_definitions() -> tuple[SearchDefinition, ...]:
    return tuple(integration_graph().search.values())


def resource_search_capabilities() -> dict[str, tuple[Capability | str, ...]]:
    return {
        spec.search.scope: spec.required_capabilities for spec in integration_graph().resources.values() if spec.search
    }


class ListOperation(TypedDict):
    """How a resource is listed, where it can be."""

    filters: Annotated[
        list[str],
        Describe(
            "The fields a listing narrows by, without `limit` and `cursor`, which page it; `describe_resources` "
            "given this name says each one's type and choices."
        ),
    ]
    fields: NotRequired[
        Annotated[
            list[str],
            Describe("The fields each item keeps unless the caller names its own; absent, items keep them all."),
        ]
    ]
    query_schema: NotRequired[
        Annotated[
            dict[str, Any],
            Describe("The JSON Schema of what a listing takes; only in a read of this one resource."),
        ]
    ]


class GetOperation(TypedDict):
    """How one record of a resource is read, where it can be."""

    identifier: Annotated[str, Describe("The field of a record that `get_resource` finds it by.")]


class SearchOperation(TypedDict):
    """How a resource is searched, where it can be."""

    scope: Annotated[str, Describe("The name search takes to look only in this resource.")]


class ResourceOperations(TypedDict):
    """What can be done with a resource: each read it serves, or null."""

    list: ListOperation | None
    get: GetOperation | None
    search: SearchOperation | None


class ResourceDescription(TypedDict):
    """One registry entry as every adapter describes it."""

    name: Annotated[str, Describe("The resource's name, which `list_resource` and `get_resource` take.")]
    label: Annotated[str, Describe("What a person calls the resource.")]
    summary: Annotated[str, Describe("What the resource holds, in a sentence.")]
    web_route: WebRoute
    required_capabilities: RequiredPermissions
    operations: ResourceOperations
    permitted: NotRequired[Permitted]


# What the catalog of every resource says in place of each list's whole schema.
WHOLE_SCHEMA = (
    "Each list's whole query_schema, with its types and choices, is describe_resources with name set to its resource."
)


# The shape describe_resources answers in; it moves when that shape does.
RESOURCES_SCHEMA_VERSION = 2


class ResourceCatalog(TypedDict):
    """What ``describe_resources`` answers."""

    schema_version: Annotated[SchemaVersion, Pinned(RESOURCES_SCHEMA_VERSION)]
    resources: Annotated[list[ResourceDescription], Describe("Each resource asked for: every one, or the one named.")]
    schemas: NotRequired[
        Annotated[str, Describe("Where each list's whole query schema is; only where the answer holds every resource.")]
    ]


def describe_resources(*, principal: Principal | None = None, name: str = "") -> ResourceCatalog:
    """Every resource HQ serves and its operations, each list by the fields it
    narrows by; or, given ``name``, that one resource with its list's whole
    query schema. Given ``principal``, each entry says whether it is
    ``permitted``, by the rule its read refuses by."""

    if name:
        return {
            "schema_version": RESOURCES_SCHEMA_VERSION,
            "resources": [_described(_resource(name), principal, whole=True)],
        }
    described = [_described(spec, principal, whole=False) for spec in integration_graph().resources.values()]
    return {"schema_version": RESOURCES_SCHEMA_VERSION, "resources": described, "schemas": WHOLE_SCHEMA}


def _listing(spec: ResourceSpec, *, whole: bool) -> ListOperation | None:
    if not spec.list_query_type:
        return None
    listing: ListOperation = {"filters": list(list_filters(spec.list_query_type))}
    if spec.list_fields:
        listing["fields"] = list(spec.list_fields)
    if whole:
        listing["query_schema"] = json_schema(spec.list_query_type)
    return listing


def _described(spec: ResourceSpec, principal: Principal | None, *, whole: bool) -> ResourceDescription:
    entry: ResourceDescription = {
        "name": spec.name,
        "label": spec.label,
        "summary": spec.summary,
        "web_route": spec.web_route or None,
        "required_capabilities": list(required_capability_names(spec)),
        "operations": {
            "list": _listing(spec, whole=whole),
            "get": ({"identifier": spec.identifier} if spec.identifier else None),
            "search": ({"scope": spec.search.scope} if spec.search else None),
        },
    }
    if principal is not None:
        entry["permitted"] = readable(spec, principal)
    return entry


def _resource(name: str) -> ResourceSpec:
    try:
        return resource_registry()[name]
    except KeyError as exc:
        raise UnknownResource(
            "HQ serves no resource by that name; describe_resources names each one.",
            details={"next": next_call("describe_resources")},
        ) from exc


def _authorize(spec: ResourceSpec, principal: Principal) -> None:
    require_all(principal, spec.required_capabilities)


def readable(spec: ResourceSpec, principal: Principal) -> bool:
    """Whether ``principal`` may read ``spec``: what ``_authorize`` refuses by, as a boolean."""

    return principal.permits(*spec.required_capabilities)


def _caller(spec: ResourceSpec, principal: Principal) -> dict[str, Principal]:
    return {"principal": principal} if spec.pass_principal else {}


def list_resource(
    name: str,
    query: dict[str, Any] | None = None,
    *,
    principal: Principal,
    strict: bool = True,
    fields: Fields | None = None,
) -> ResourceCollection:
    """One page of a resource; ``fields`` is a field mask read in each item,
    ``["*"]`` the whole item, and none its kind's ``list_fields``."""

    spec = _resource(name)
    _authorize(spec, principal)
    if not spec.list_handler or not spec.list_query_type:
        raise UnsupportedResourceOperation(
            "This resource cannot be listed; details.next is what it does serve.", details=_served(spec)
        )
    try:
        parsed = spec.list_query_type.model_validate(query or {}, strict=strict)
    except ValidationError as exc:
        raise InvalidResourceInput(name, exc.errors()) from exc
    asked = parsed.model_dump()
    token = asked.pop(PAGE_FIELD, None)
    bound = _bound(parsed)
    with paging(_opened(name, token, bound) if token else None) as scope:
        try:
            result = spec.list_handler(**asked, **_caller(spec, principal))
        except CursorRefusal as exc:
            raise _cursor_refused(name, exc.reason) from exc
    # A schema that offers a cursor is a promise every full page issues one.
    if PAGE_FIELD in type(parsed).model_fields and not scope.claimed:
        raise RuntimeError(f"Resource {name!r} declares a cursor its handler never paged by.")
    invalid = RuntimeError(f"Resource {name!r} list handler returned an invalid collection.")
    try:
        handled = _HANDLED.validate_python(result, strict=True)
    except ValidationError:
        raise invalid from None
    if handled["count"] != len(handled["items"]):
        raise invalid
    page: ResourceCollection = {**handled, **_page(spec, parsed, handled, scope, bound)}
    if fields is None:
        return {**page, "items": _listed_fields(spec, page["items"])}
    if asks_whole(fields):
        return page
    return {**page, "items": _masked(name, page["items"], fields)}


def _listed_fields(spec: ResourceSpec, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each item cut to its kind's ``list_fields``, or whole where it declares none.

    A field an item lacks is left out rather than refused: it is the kind's
    declaration, not the caller's ask, and a page of items that happen to lack
    an optional one is still a page.
    """

    if not spec.list_fields:
        return items
    return [{key: item[key] for key in item if key in spec.list_fields} for item in items]


class PagePosition(TypedDict):
    """Where a page of a list stopped."""

    total: Annotated[
        int | None, Describe("How many records matched in all, where the list counts them cheaply; else null.")
    ]
    truncated: Annotated[
        bool, Describe("Whether more records follow this page; `next` reads the page after, and is null on the last.")
    ]
    next: NextPage | None


@with_config(ConfigDict(extra="allow"))
class ResourceCollection(PagePosition):
    """One page of a list: every list holds these, and may hold metadata of its own."""

    items: Annotated[
        list[dict[str, Any]], Describe("The page's records, in the list's order, each in its row's shape.")
    ]
    count: Annotated[int, Describe("How many records this page holds.")]


@with_config(ConfigDict(extra="allow"))
class _HandledPage(TypedDict):
    """What a list handler answers, before HQ says where its page stopped."""

    items: list[dict[str, Any]]
    count: int
    total: NotRequired[int | None]
    truncated: NotRequired[bool]


_HANDLED = TypeAdapter(_HandledPage)


def _page(spec: ResourceSpec, parsed: BaseModel, result: _HandledPage, scope: PageScope, bound: str) -> PagePosition:
    """Whether more follow this page, and the cursor that reads the next one.

    ``total`` is what the handler counted, or None where counting is not cheap.
    A page cut through ``projection``'s helpers says where it stopped; one cut
    by hand is read as cut off when its page is full, so a full page is never
    mistaken for the whole list, and offers no cursor, since nothing could
    resume it.
    """

    count, total = result["count"], result.get("total")
    if scope.claimed and PAGE_FIELD in type(parsed).model_fields:
        more = scope.after is not None
        return {
            "total": total,
            "truncated": more,
            "next": {"cursor": _issued(spec.name, bound, scope.after)} if more else None,
        }
    # A list's query model is its declarer's, plugin or host, and need not page.
    limit = getattr(parsed, "limit", None)
    full = isinstance(limit, int) and count >= page_size(limit)
    truncated = result.get("truncated", total > count if isinstance(total, int) else full)
    return {"total": total, "truncated": truncated, "next": None}


# A cursor is signed, so the position in it is one HQ wrote, and carries the
# kind and the filters it was issued under, so a page is never read past a
# position in some other list.
_CURSOR_SALT = "hq.platform.application.resources.cursor"


def _bound(parsed: BaseModel) -> str:
    """The filters a page was read under, as one digest: every query field but the page's size and cursor."""

    terms = parsed.model_dump(mode="json", exclude={"limit", PAGE_FIELD})
    return hashlib.sha256(json.dumps(terms, sort_keys=True).encode()).hexdigest()[:32]


def _issued(name: str, bound: str, position: dict[str, Any] | None) -> str:
    return signing.dumps({"kind": name, "bound": bound, "at": position}, salt=_CURSOR_SALT, compress=True)


def _opened(name: str, token: str, bound: str) -> dict[str, Any]:
    """The position ``token`` resumes from, or a refusal naming the cursor when it
    was not issued by this kind's list under these filters."""

    try:
        opened = signing.loads(token, salt=_CURSOR_SALT)
    except signing.BadSignature as exc:
        raise _cursor_refused(name, "cursor is not one HQ issued; send a page's next.cursor as it came.") from exc
    if not isinstance(opened, dict) or opened.get("kind") != name or not isinstance(opened.get("at"), dict):
        raise _cursor_refused(name, "cursor was issued for another list.")
    if opened.get("bound") != bound:
        raise _cursor_refused(name, "cursor was issued under other filters; send the filters it was issued with.")
    position: dict[str, Any] = opened["at"]
    return position


def _cursor_refused(name: str, reason: str) -> InvalidResourceInput:
    return InvalidResourceInput(name, [{"type": "value_error", "loc": [PAGE_FIELD], "msg": reason}])


def _masked(name: str, records: list[Any], fields: Fields) -> list[dict[str, Any]]:
    if any(not isinstance(record, dict) for record in records):
        raise TypeError(f"Resource {name!r} returned a record that is not an object.")
    try:
        return selected(records, fields)
    except FieldMaskRefusal as exc:
        raise InvalidResourceInput(name, exc.errors) from exc


def get_resource(
    name: str,
    identifier: object,
    *,
    principal: Principal,
    strict: bool = True,
    fields: Fields | None = None,
) -> dict[str, Any]:
    """One record of a resource; ``fields`` is a field mask over it.

    A record that is not there raises ``RecordNotFound``, whichever handler
    looked for it.
    """

    spec = _resource(name)
    _authorize(spec, principal)
    if not spec.detail_handler or not spec.identifier:
        raise UnsupportedResourceOperation(
            "This resource has no detail view; details.next is what it does serve.", details=_served(spec)
        )
    try:
        parsed: Any = TypeAdapter(spec.identifier_type).validate_python(identifier, strict=strict)
    except ValidationError as exc:
        raise InvalidResourceInput(name, exc.errors()) from exc
    try:
        result = spec.detail_handler(parsed, **_caller(spec, principal))
    except RecordNotFound as exc:
        # The host's own sentence, with what finds the record beside it.
        exc.details = _found_by(spec)
        raise
    except spec.not_found_errors as exc:
        # An extension declares these types; their text is written for the
        # extension, not for a caller, so answer with this module's own.
        raise RecordNotFound("No record matches the requested identifier." + FOUND_BY, details=_found_by(spec)) from exc
    if not isinstance(result, dict):
        raise TypeError(f"Resource {name!r} detail handler returned a non-object.")
    if fields is None or asks_whole(fields):
        return result
    return _masked(name, [result], fields)[0]
