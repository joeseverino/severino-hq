"""Discoverable, authorized read resources shared by every HQ adapter."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, TypedDict, override

from django.db.models import Model
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.providers import spec_withheld
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
from .contracts import DOTTED_NAME
from .input_errors import pydantic_refusal
from .integration_specs import ResourceSpec
from .integration_validation import required_capability_names
from .integrations import integration_graph
from .record_errors import RecordNotFound
from .search_contracts import SearchDefinition, search_lines
from .security import Capability, Principal, require_all


class ResourceSearchDefinition(SearchDefinition):
    """A declaration's result opens the machine, service or domain it belongs to."""

    @override
    def url(self, instance: Any) -> str:
        return services.home_url(instance)

    @override
    def body(self, instance: Model) -> str:
        """The indexed text; a withheld kind's spec is left out (``ProviderSpec.withheld``)."""

        fields = tuple(
            field for field in self.fields if field != "spec" or not spec_withheld(str(getattr(instance, "kind", "")))
        )
        return "\n".join(line for field in fields for line in search_lines(getattr(instance, field, None)))


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
        "Records",
        "Everything HQ manages: what HQ expects and what is live.",
        Capability.READ,
        infrastructure.list_managed_resources,
        InfrastructureResourceQuery,
        resource_context.get_managed_resource,
        "key",
        search=ResourceSearchDefinition(
            "infrastructure.resources",
            ManagedResource,
            "key",
            ("key", "kind", "spec", "status", "conditions"),
            label="Records",
            title_field="key",
            badge_field="kind_label",
            snippet_field="search_summary",
        ),
        web_route="control_plane:list",
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
        "Needs you",
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
        web_route="control_plane:containers",
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
    ),
    ResourceSpec(
        "paths",
        "Request paths",
        "The path a request for a hostname takes, hop by hop, with each hop's reading and certificate.",
        Capability.READ,
        detail_handler=derived_reads.get_path,
        identifier="hostname",
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
        "hq",
        "HQ",
        "HQ itself: the release it runs and whether its branch is ahead, the machine and "
        "container it runs in, its background jobs, what it can read and its open problems.",
        Capability.READ,
        hq_context.read_hq,
        EmptyQuery,
        web_route="hq_self",
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
    ),
    ResourceSpec(
        "connection.standing",
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


def resource_registry() -> dict[str, ResourceSpec]:
    return dict(integration_graph().resources)


def resource_search_definitions() -> tuple[SearchDefinition, ...]:
    return tuple(integration_graph().search.values())


def resource_search_capabilities() -> dict[str, tuple[Capability | str, ...]]:
    return {
        spec.search.scope: spec.required_capabilities for spec in integration_graph().resources.values() if spec.search
    }


class ListOperation(TypedDict):
    query_schema: dict[str, Any]


class GetOperation(TypedDict):
    identifier: str


class SearchOperation(TypedDict):
    scope: str


class ResourceOperations(TypedDict):
    list: ListOperation | None
    get: GetOperation | None
    search: SearchOperation | None


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
                "list": ({"query_schema": spec.list_query_type.model_json_schema()} if spec.list_query_type else None),
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


Fields = list[str] | tuple[str, ...]
# A field mask (AIP-161): each name keeps its whole value, or the names under
# it keep only those parts. ``None`` is a whole value.
type Mask = dict[str, Mask | None]
_FIELD_PATH = re.compile(r"[^.]+(?:\.[^.]+)*")


@dataclass(frozen=True, slots=True)
class _Selection:
    resource: str
    mask: Mask
    # The first field that names each path, so a refusal points at it.
    origin: dict[tuple[str, ...], int]

    def refuse(self, path: tuple[str, ...], error_type: str, msg: str, *, at: tuple[str, ...] = ()) -> NoReturn:
        loc = ["fields", self.origin[path], *at]
        raise InvalidResourceInput(self.resource, [{"type": error_type, "loc": loc, "msg": msg}])


def _selection(name: str, fields: Fields) -> _Selection:
    """The paths as one mask: shared prefixes merge, and a whole value wins."""

    if not fields:
        raise InvalidResourceInput(
            name, [{"type": "too_short", "loc": ["fields"], "msg": "List should have at least 1 item"}]
        )
    if malformed := [index for index, field in enumerate(fields) if not _FIELD_PATH.fullmatch(field)]:
        raise InvalidResourceInput(
            name,
            [
                {"type": "string_pattern_mismatch", "loc": ["fields", index], "msg": "String should match 'name.name'"}
                for index in malformed
            ],
        )
    mask: Mask = {}
    origin: dict[tuple[str, ...], int] = {}
    for index, field in enumerate(fields):
        segments = field.split(".")
        for depth in range(len(segments)):
            origin.setdefault(tuple(segments[: depth + 1]), index)
        _merge(mask, segments)
    return _Selection(name, mask, origin)


def _merge(mask: Mask, segments: list[str]) -> None:
    *parents, leaf = segments
    node = mask
    for segment in parents:
        if segment in node and node[segment] is None:
            return  # The whole value is already kept.
        child = node.get(segment)
        if child is None:
            child = node[segment] = {}
        node = child
    node[leaf] = None


def _cut(selection: _Selection, records: list[dict[str, Any]], mask: Mask, at: tuple[str, ...]) -> list[dict[str, Any]]:
    """``records`` cut to ``mask``, every record at this depth checked together.

    A name no record here carries is refused with the names there are, so a
    typo is never answered with records that silently lack it. With no records
    there is nothing to name a key against.
    """

    known = list(dict.fromkeys(key for record in records for key in record))
    if records and (unknown := [key for key in mask if key not in known]):
        choices = ", ".join(f"'{key}'" for key in known)
        selection.refuse((*at, unknown[0]), "literal_error", f"Input should be {choices}")
    cut = [{key: value for key, value in record.items() if key in mask and mask[key] is None} for record in records]
    for key, inner in mask.items():
        if inner is not None:
            _cut_within(selection, records, cut, key, inner, (*at, key))
    return [{key: out[key] for key in record if key in out} for record, out in zip(records, cut, strict=True)]


def _cut_within(
    selection: _Selection,
    records: list[dict[str, Any]],
    cut: list[dict[str, Any]],
    key: str,
    inner: Mask,
    path: tuple[str, ...],
) -> None:
    """Cut the objects under ``key``, one object or each element of a list."""

    elements: list[dict[str, Any]] = []
    for record in records:
        value = record.get(key)
        if isinstance(value, dict):
            elements.append(value)
        elif isinstance(value, list) and all(isinstance(element, dict) for element in value):
            elements.extend(value)
        elif key in record and value is not None:
            selection.refuse(path, "dict_type", "Input should be a valid dictionary", at=path)
    # The cut elements, put back where each came from.
    shaped = iter(_cut(selection, elements, inner, path))
    for record, out in zip(records, cut, strict=True):
        value = record.get(key)
        if isinstance(value, dict):
            out[key] = next(shaped)
        elif isinstance(value, list):
            out[key] = [next(shaped) for _ in value]
        elif key in record:
            out[key] = None


def _selected(name: str, records: list[Any], fields: Fields) -> list[dict[str, Any]]:
    """Each record cut to the field mask ``fields`` names, in its own order."""

    if any(not isinstance(record, dict) for record in records):
        raise RuntimeError(f"Resource {name!r} returned a record that is not an object.")
    selection = _selection(name, fields)
    return _cut(selection, records, selection.mask, ())


def list_resource(
    name: str,
    query: dict[str, Any] | None = None,
    *,
    principal: Principal,
    strict: bool = True,
    fields: Fields | None = None,
) -> dict[str, Any]:
    """One page of a resource; ``fields`` is a field mask read in each item."""

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
        raise RuntimeError(f"Resource {name!r} list handler returned an invalid collection.")
    if fields is None:
        return result
    return {**result, "items": _selected(name, result["items"], fields)}


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
        raise UnsupportedResourceOperation(f"Resource {name!r} has no detail view.")
    try:
        parsed: Any = TypeAdapter(spec.identifier_type).validate_python(identifier, strict=strict)
    except ValidationError as exc:
        raise InvalidResourceInput(name, exc.errors()) from exc
    try:
        result = spec.detail_handler(parsed, **_caller(spec, principal))
    except spec.not_found_errors as exc:
        # An extension declares these types; their text is written for the
        # extension, not for a caller, so answer with this module's own.
        raise RecordNotFound("No record matches the requested identifier.") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Resource {name!r} detail handler returned a non-object.")
    if fields is None:
        return result
    return _selected(name, [result], fields)[0]
