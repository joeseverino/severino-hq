"""Discoverable, authorized read resources shared by every HQ adapter."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from control_plane.models import ManagedResource
from core.models import AuditLog

from . import (
    connection_context,
    container_reads,
    derived_reads,
    infrastructure,
    resource_context,
    service_list,
    services,
    tailnet_context,
)
from .contracts import DOTTED_NAME
from .integration_specs import ResourceSpec
from .integrations import integration_graph
from .input_errors import pydantic_refusal
from .integration_validation import required_capability_names
from .search_contracts import SearchDefinition
from .security import Capability, Principal, require_all


class ResourceSearchDefinition(SearchDefinition):
    """A declaration's result opens the machine, service or domain it belongs to."""

    def url(self, instance: Any) -> str:
        return services.home_url(instance)


class ResourceQuery(BaseModel):
    """Base for list inputs: unknown fields are always programmer errors."""

    model_config = ConfigDict(extra="forbid")


class BoundedQuery(ResourceQuery):
    # The shared projection layer applies the deployment-wide ceiling. The
    # query schema rejects nonsensical pages without giving this adapter a
    # second, drift-prone copy of that maximum.
    limit: int = Field(default=50, ge=1)


class InfrastructureResourceQuery(BoundedQuery):
    kind: str | None = None
    kinds: str | None = Field(default=None, max_length=2000)

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
        return value


class EmptyQuery(ResourceQuery):
    pass


class ActionItemQuery(BoundedQuery):
    query: str = Field(default="", max_length=200)
    status: str = ""
    source: str = ""


class ReadingQuery(BoundedQuery):
    provider: str | None = None


class SearchQuery(BoundedQuery):
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=20, ge=1)


CORE_RESOURCE_SPECS = (
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
        "infrastructure.resources",
        "Infrastructure resources",
        "Canonical desired and observed infrastructure state.",
        Capability.READ,
        infrastructure.list_managed_resources,
        InfrastructureResourceQuery,
        resource_context.get_managed_resource,
        "key",
        not_found_errors=(infrastructure.NotFoundError,),
        search=ResourceSearchDefinition(
            "infrastructure.resources",
            ManagedResource,
            "key",
            ("key", "kind", "spec", "status", "conditions"),
            label="Infrastructure resources",
            title_field="key",
            badge_field="kind_label",
            snippet_field="search_summary",
        ),
        web_route="control_plane:list",
    ),
    ResourceSpec(
        "services",
        "Services",
        "Declared hostnames and the state of their DNS, ingress, and TLS.",
        Capability.READ,
        service_list.list_services,
        EmptyQuery,
        service_list.get_service,
        "hostname",
        not_found_errors=(service_list.NotFoundError,),
        web_route="control_plane:services",
    ),
    ResourceSpec(
        "estate",
        "Estate",
        "Machines online, services, domains, next renewals and connections needing attention.",
        Capability.READ,
        derived_reads.list_estate,
        EmptyQuery,
        web_route="dashboard",
    ),
    ResourceSpec(
        "action.items",
        "Action items",
        "The composed action queue: what needs doing, from every domain.",
        Capability.READ,
        derived_reads.list_action_items,
        ActionItemQuery,
        web_route="action_items",
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
        not_found_errors=(derived_reads.NotFoundError,),
        web_route="control_plane:machines",
    ),
    ResourceSpec(
        "containers",
        "Containers",
        "Every running container: what it runs and whether that is current and safe, "
        "how it is run, and its posture against the container standard.",
        Capability.READ,
        container_reads.list_containers,
        BoundedQuery,
        container_reads.get_container,
        "address",
        not_found_errors=(container_reads.NotFoundError,),
        web_route="control_plane:containers",
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
        not_found_errors=(container_reads.NotFoundError,),
        web_route="control_plane:containers",
    ),
    ResourceSpec(
        "domains",
        "Domains",
        "Every domain HQ declares or a sweep has seen, its services and registration.",
        Capability.READ,
        derived_reads.list_domains,
        BoundedQuery,
        derived_reads.get_domain,
        "name",
        not_found_errors=(derived_reads.NotFoundError,),
        web_route="zones:index",
    ),
    ResourceSpec(
        "relationships",
        "Relationships",
        "One machine, service, domain or declaration and everything related to it.",
        Capability.READ,
        detail_handler=derived_reads.get_relationships,
        identifier="node",
        not_found_errors=(derived_reads.NotFoundError,),
        web_route="control_plane:topology",
        pass_principal=True,
    ),
    ResourceSpec(
        "paths",
        "Request paths",
        "The path a request for a hostname takes, hop by hop, with each hop's reading and certificate.",
        Capability.READ,
        detail_handler=derived_reads.get_path,
        identifier="hostname",
        not_found_errors=(derived_reads.NotFoundError,),
        web_route="control_plane:services",
    ),
    ResourceSpec(
        "request.path",
        "Request path",
        "How the calling request reached HQ: HQ's own path hop by hop, each hop "
        "checked against what the request shows, with the admission layers decided there.",
        Capability.READ,
        derived_reads.list_request_path,
        EmptyQuery,
        web_route="connection",
    ),
    ResourceSpec(
        "readings",
        "Readings",
        "Each reading kind, its last read, and its stored records as its schema admits them.",
        Capability.READ,
        derived_reads.list_readings,
        ReadingQuery,
        derived_reads.get_reading,
        "kind",
        not_found_errors=(derived_reads.NotFoundError,),
        web_route="control_plane:connections",
    ),
    ResourceSpec(
        "credentials",
        "Credential sight",
        "What each connection provider's credential can see, and what it would need to see more.",
        Capability.READ,
        derived_reads.list_credentials,
        EmptyQuery,
        derived_reads.get_credential,
        "provider",
        not_found_errors=(derived_reads.NotFoundError,),
        web_route="control_plane:connections",
    ),
    ResourceSpec(
        "tailnet",
        "Tailnet",
        "The tailnet policy: settings, grants, shell rules, groups and tags with their "
        "machines, services, app connectors, tests, findings, and what could not be read.",
        Capability.READ,
        tailnet_context.get_tailnet,
        EmptyQuery,
        web_route="control_plane:tailnet",
        pass_principal=True,
    ),
    ResourceSpec(
        "connection.standing",
        "Connection standing",
        "Each connection with what its credential sees, how fresh each reading is, what "
        "was refused and the fix; the summary counts, the security posture and HQ's path.",
        Capability.READ,
        connection_context.list_connection_standing,
        EmptyQuery,
        connection_context.get_connection_standing,
        "connection_ref",
        not_found_errors=(derived_reads.NotFoundError,),
        web_route="control_plane:connections",
        pass_principal=True,
    ),
    ResourceSpec(
        "search",
        "Search",
        "Machines, services and domains, then records, matching a query.",
        Capability.READ,
        derived_reads.search,
        SearchQuery,
        web_route="search",
        pass_principal=True,
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


class UnknownResource(ResourceError):
    pass


class UnsupportedResourceOperation(ResourceError):
    pass


class InvalidResourceInput(ResourceError):
    def __init__(self, name: str, errors: Iterable[Mapping[str, Any]]) -> None:
        refusal = pydantic_refusal(name, errors)
        super().__init__(refusal.message)
        self.errors = refusal.details


class ResourceNotFound(ResourceError):
    pass


def resource_registry() -> dict[str, ResourceSpec]:
    return dict(integration_graph().resources)


def resource_search_definitions() -> tuple[SearchDefinition, ...]:
    return tuple(integration_graph().search.values())


def resource_search_capabilities() -> dict[str, tuple[Capability | str, ...]]:
    return {
        spec.search.scope: spec.required_capabilities
        for spec in integration_graph().resources.values()
        if spec.search
    }


class ListOperation(TypedDict):
    query_schema: dict[str, Any]


class GetOperation(TypedDict):
    identifier: str


class SearchOperation(TypedDict):
    scope: str


# Functional form: ``list`` is a key here, not the builtin.
ResourceOperations = TypedDict(
    "ResourceOperations",
    {
        "list": ListOperation | None,
        "get": GetOperation | None,
        "search": SearchOperation | None,
    },
)


class ResourceDescription(TypedDict):
    """One registry entry as every adapter describes it."""

    name: str
    label: str
    summary: str
    web_route: str | None
    required_capabilities: list[str]
    operations: ResourceOperations


def describe_resources() -> dict[str, Any]:
    described: list[ResourceDescription] = [
        {
            "name": spec.name,
            "label": spec.label,
            "summary": spec.summary,
            "web_route": spec.web_route or None,
            "required_capabilities": list(required_capability_names(spec)),
            "operations": {
                "list": (
                    {"query_schema": spec.list_query_type.model_json_schema()}
                    if spec.list_query_type
                    else None
                ),
                "get": ({"identifier": spec.identifier} if spec.identifier else None),
                "search": ({"scope": spec.search.scope} if spec.search else None),
            },
        }
        for spec in integration_graph().resources.values()
    ]
    return {"ok": True, "schema_version": 1, "resources": described}


def _resource(name: str) -> ResourceSpec:
    try:
        return resource_registry()[name]
    except KeyError as exc:
        raise UnknownResource(f"Unknown resource {name!r}.") from exc


def _authorize(spec: ResourceSpec, principal: Principal) -> None:
    require_all(principal, spec.required_capabilities)


def _caller(spec: ResourceSpec, principal: Principal) -> dict[str, Principal]:
    return {"principal": principal} if spec.pass_principal else {}


def list_resource(
    name: str,
    query: dict[str, Any] | None = None,
    *,
    principal: Principal,
    strict: bool = True,
) -> dict[str, Any]:
    spec = _resource(name)
    _authorize(spec, principal)
    if not spec.list_handler or not spec.list_query_type:
        raise UnsupportedResourceOperation(f"Resource {name!r} cannot be listed.")
    try:
        parsed = spec.list_query_type.model_validate(query or {}, strict=strict)
    except ValidationError as exc:
        raise InvalidResourceInput(name, exc.errors()) from exc
    result = spec.list_handler(**parsed.model_dump(), **_caller(spec, principal))
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("items"), list)
        or not isinstance(result.get("count"), int)
        or result["count"] != len(result["items"])
    ):
        raise RuntimeError(
            f"Resource {name!r} list handler returned an invalid collection."
        )
    return result


def get_resource(
    name: str, identifier: Any, *, principal: Principal, strict: bool = True
) -> dict[str, Any]:
    spec = _resource(name)
    _authorize(spec, principal)
    if not spec.detail_handler or not spec.identifier:
        raise UnsupportedResourceOperation(f"Resource {name!r} has no detail view.")
    try:
        parsed: Any = TypeAdapter(spec.identifier_type).validate_python(
            identifier, strict=strict
        )
    except ValidationError as exc:
        raise InvalidResourceInput(name, exc.errors()) from exc
    try:
        result = spec.detail_handler(parsed, **_caller(spec, principal))
    except spec.not_found_errors as exc:
        # A provider declares these types; their text is written for the
        # provider, not for a caller, so answer with this module's own.
        raise ResourceNotFound(
            f"No {name!r} record matches the requested identifier."
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Resource {name!r} detail handler returned a non-object.")
    return result
