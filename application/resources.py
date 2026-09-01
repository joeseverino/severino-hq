"""Discoverable, authorized read resources shared by every HQ adapter."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Callable

from django.core.exceptions import ImproperlyConfigured
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from assets.models import Asset
from content.models import ContentItem
from control_plane.models import ManagedResource
from core.models import AuditLog
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt

from . import (
    analytics,
    assets,
    contact_submissions,
    infrastructure,
    projects,
    read_models,
    services,
)
from .contracts import DJANGO_ROUTE, DOTTED_NAME
from .plugins import plugin_resource_specs, plugin_search_definitions
from .search_contracts import SearchDefinition
from .security import Capability, Principal

RESOURCE_NAME = DOTTED_NAME


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


class ProjectQuery(BoundedQuery):
    status: str | None = None
    query: str | None = None


class AssetQuery(BoundedQuery):
    status: str | None = None
    query: str | None = None


class ExpenseQuery(BoundedQuery):
    year: int | None = None
    category: str | None = None


class ReceiptQuery(BoundedQuery):
    unmatched_only: bool = False


class AnalyticsQuery(BoundedQuery):
    # Empty means the default breakdown rather than "every breakdown at once":
    # the dimensions cannot be crossed, so a combined answer would be six
    # answers wearing one collection's shape.
    dimension: str = ""
    days: int = Field(default=28, ge=1, le=184)


class EmptyQuery(ResourceQuery):
    pass


class ContactSubmissionQuery(BoundedQuery):
    status: str = ""
    query: str = ""


@dataclass(frozen=True)
class ResourceSpec:
    """One declaration of a readable domain and every operation it supports."""

    name: str
    label: str
    summary: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    list_handler: Callable[..., dict[str, Any]] | None = None
    list_query_type: type[BaseModel] | None = None
    detail_handler: Callable[[Any], dict[str, Any]] | None = None
    identifier: str | None = None
    identifier_type: type = str
    not_found_errors: tuple[type[Exception], ...] = ()
    search: SearchDefinition | None = None
    web_route: str = ""

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)


CORE_RESOURCE_SPECS = (
    ResourceSpec(
        "projects",
        "Projects",
        "Projects and their safe cross-domain relationships.",
        Capability.READ,
        projects.list_projects,
        ProjectQuery,
        projects.get_project,
        "slug",
        not_found_errors=(projects.NotFoundError,),
        search=SearchDefinition(
            "projects",
            Project,
            "slug",
            ("name", "slug", "description", "technologies_used", "notes"),
            label="Projects",
            title_field="name",
        ),
        web_route="projects:list",
    ),
    ResourceSpec(
        "contact.submissions",
        "Contact submissions",
        "Contact requests held in Cloudflare D1 and reviewed through HQ.",
        Capability.MANAGE_CONTACTS,
        contact_submissions.list_contact_submissions,
        ContactSubmissionQuery,
        contact_submissions.get_contact_submission,
        "id",
        int,
        not_found_errors=(contact_submissions.ContactSubmissionNotFound,),
        web_route="contacts:list",
    ),
    ResourceSpec(
        "assets",
        "Assets",
        "Assets and their safe cross-domain relationships.",
        Capability.READ,
        assets.list_assets,
        AssetQuery,
        assets.get_asset,
        "slug",
        not_found_errors=(assets.NotFoundError,),
        search=SearchDefinition(
            "assets",
            Asset,
            "slug",
            ("item_name", "slug", "vendor", "serial_number", "category", "notes"),
            label="Assets",
            title_field="item_name",
        ),
        web_route="assets:list",
    ),
    ResourceSpec(
        "content",
        "Content",
        "Content records indexed by HQ.",
        Capability.READ,
        search=SearchDefinition(
            "content",
            ContentItem,
            "slug",
            ("title", "slug", "topic", "tags", "notes"),
            label="Content",
            title_field="title",
        ),
        web_route="content:list",
    ),
    ResourceSpec(
        "documentation",
        "Docs",
        "Sensitivity-aware documentation pointers indexed by HQ.",
        Capability.READ,
        search=SearchDefinition(
            "documentation",
            DocumentationRecord,
            "doc_id",
            (
                "doc_id",
                "title",
                "system_service",
                "obsidian_path",
                "github_path",
                "notes",
            ),
            label="Docs",
            title_field="title",
            badge_field="doc_id",
        ),
        web_route="docs_index:list",
    ),
    ResourceSpec(
        "expenses",
        "Expenses",
        "Expense records with stable relationship identifiers.",
        Capability.READ,
        read_models.list_expenses,
        ExpenseQuery,
        search=SearchDefinition(
            "expenses",
            Expense,
            "pk",
            ("vendor", "item", "category", "business_purpose", "notes"),
            label="Expenses",
        ),
        web_route="expenses:list",
    ),
    ResourceSpec(
        "receipts",
        "Receipts",
        "Receipt metadata without file contents, storage paths, or URLs.",
        Capability.READ,
        read_models.list_receipts,
        ReceiptQuery,
        search=SearchDefinition(
            "receipts",
            Receipt,
            "pk",
            ("original_filename", "vendor", "notes"),
            label="Receipts",
        ),
        web_route="receipts:list",
    ),
    ResourceSpec(
        "analytics",
        "Analytics",
        "What the published site was asked for, by any breakdown HQ records.",
        Capability.READ,
        analytics.list_analytics,
        AnalyticsQuery,
        web_route="analytics:overview",
    ),
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
            timestamp_field="created_at",
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
        infrastructure.get_managed_resource,
        "key",
        not_found_errors=(infrastructure.NotFoundError,),
        search=SearchDefinition(
            "infrastructure.resources",
            ManagedResource,
            "key",
            ("key", "kind", "spec", "status", "conditions"),
            label="Infrastructure resources",
            title_field="key",
            badge_field="kind",
        ),
        web_route="control_plane:list",
    ),
    ResourceSpec(
        "services",
        "Services",
        "Declared hostnames and the state of their DNS, ingress, and TLS.",
        Capability.READ,
        services.list_services,
        EmptyQuery,
        services.get_service,
        "hostname",
        not_found_errors=(services.NotFoundError,),
        web_route="control_plane:services",
    ),
)


class ResourceError(ValueError):
    """Base for resource failures: ``reason`` is this module's own text.

    Adapters answer a caller with ``reason``, never ``str(exc)`` -- a relayed
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
    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("Resource input did not match its schema.")
        self.errors = errors


class ResourceNotFound(ResourceError):
    pass


def _capability_names(spec: ResourceSpec) -> tuple[str, ...]:
    return tuple(
        item.value if isinstance(item, Capability) else item
        for item in spec.required_capabilities
    )


def _validate_resource_identity(spec: ResourceSpec) -> None:
    if not isinstance(spec, ResourceSpec):
        raise ImproperlyConfigured(
            "A resource provider returned something other than ResourceSpec."
        )
    if not RESOURCE_NAME.fullmatch(spec.name):
        raise ImproperlyConfigured(f"Invalid resource name {spec.name!r}.")
    if not spec.label.strip() or not spec.summary.strip():
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} must declare a label and summary."
        )
    required = _capability_names(spec)
    if (
        not required
        or len(required) != len(set(required))
        or any(not RESOURCE_NAME.fullmatch(item) for item in required)
    ):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} must declare unique valid capabilities."
        )


def _validate_list_contract(spec: ResourceSpec) -> None:
    if bool(spec.list_handler) != bool(spec.list_query_type):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} must declare its list handler and query together."
        )
    if spec.list_query_type and not (
        inspect.isclass(spec.list_query_type)
        and issubclass(spec.list_query_type, BaseModel)
        and callable(spec.list_handler)
    ):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} has an invalid list contract."
        )
    if spec.list_handler and spec.list_query_type:
        try:
            values = spec.list_query_type().model_dump()
            inspect.signature(spec.list_handler).bind(**values)
        except (TypeError, ValidationError) as exc:
            raise ImproperlyConfigured(
                f"Resource {spec.name!r} list handler does not implement its query contract."
            ) from exc


def _validate_detail_contract(spec: ResourceSpec) -> None:
    if bool(spec.detail_handler) != bool(spec.identifier):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} must declare its detail handler and identifier together."
        )
    if spec.detail_handler:
        try:
            inspect.signature(spec.detail_handler).bind(None)
        except TypeError as exc:
            raise ImproperlyConfigured(
                f"Resource {spec.name!r} detail handler does not accept one identifier."
            ) from exc
        if not isinstance(spec.identifier_type, type):
            raise ImproperlyConfigured(
                f"Resource {spec.name!r} has an invalid identifier type."
            )
        if any(
            not isinstance(error, type) or not issubclass(error, Exception)
            for error in spec.not_found_errors
        ):
            raise ImproperlyConfigured(
                f"Resource {spec.name!r} has invalid not-found errors."
            )


def _validate_search_contract(spec: ResourceSpec) -> None:
    if not any((spec.list_handler, spec.detail_handler, spec.search)):
        raise ImproperlyConfigured(f"Resource {spec.name!r} exposes no operations.")
    if spec.search and not isinstance(spec.search, SearchDefinition):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} has an invalid search definition."
        )
    if spec.search and spec.search.scope != spec.name:
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} search scope must use the same name."
        )
    if spec.web_route and not DJANGO_ROUTE.fullmatch(spec.web_route):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} has invalid web route {spec.web_route!r}."
        )


def _validate_resource_spec(spec: ResourceSpec) -> None:
    _validate_resource_identity(spec)
    _validate_list_contract(spec)
    _validate_detail_contract(spec)
    _validate_search_contract(spec)


def _collect_resources() -> tuple[ResourceSpec, ...]:
    specs = (*CORE_RESOURCE_SPECS, *plugin_resource_specs())
    for spec in specs:
        _validate_resource_spec(spec)
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ImproperlyConfigured(
            "Duplicate resource name across HQ core and plugins."
        )
    return specs


def resource_registry() -> dict[str, ResourceSpec]:
    from .integrations import integration_graph

    return dict(integration_graph().resources)


def resource_search_definitions() -> tuple[SearchDefinition, ...]:
    from .integrations import integration_graph

    definitions = tuple(
        spec.search for spec in integration_graph().resources.values() if spec.search
    )
    # Retain the v1 search-only plugin hook while resources become the canonical
    # declaration. A plugin may migrate independently, but cannot emit a scope twice.
    definitions += tuple(plugin_search_definitions())
    if any(not isinstance(definition, SearchDefinition) for definition in definitions):
        raise ImproperlyConfigured(
            "A search provider returned something other than SearchDefinition."
        )
    scopes = [definition.scope for definition in definitions]
    if any(not RESOURCE_NAME.fullmatch(scope) for scope in scopes):
        raise ImproperlyConfigured("Every search definition must use a valid scope.")
    if len(scopes) != len(set(scopes)):
        raise ImproperlyConfigured("Duplicate search scope across HQ core and plugins.")
    return definitions


def resource_search_capabilities() -> dict[str, tuple[Capability | str, ...]]:
    from .integrations import integration_graph

    return {
        spec.search.scope: spec.required_capabilities
        for spec in integration_graph().resources.values()
        if spec.search
    }


def describe_resources() -> dict[str, Any]:
    from .integrations import integration_graph

    return {
        "ok": True,
        "schema_version": 1,
        "resources": [
            {
                "name": spec.name,
                "label": spec.label,
                "summary": spec.summary,
                "web_route": spec.web_route or None,
                "required_capabilities": list(_capability_names(spec)),
                "operations": {
                    "list": (
                        {
                            "query_schema": spec.list_query_type.model_json_schema(),
                        }
                        if spec.list_query_type
                        else None
                    ),
                    "get": (
                        {"identifier": spec.identifier} if spec.identifier else None
                    ),
                    "search": ({"scope": spec.search.scope} if spec.search else None),
                },
            }
            for spec in integration_graph().resources.values()
        ],
    }


def _resource(name: str) -> ResourceSpec:
    try:
        return resource_registry()[name]
    except KeyError as exc:
        raise UnknownResource(f"Unknown resource {name!r}.") from exc


def _authorize(spec: ResourceSpec, principal: Principal) -> None:
    for capability in spec.required_capabilities:
        principal.require(capability)


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
        raise InvalidResourceInput(exc.errors(include_url=False)) from exc
    result = spec.list_handler(**parsed.model_dump())
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
        parsed = TypeAdapter(spec.identifier_type).validate_python(
            identifier, strict=strict
        )
    except ValidationError as exc:
        raise InvalidResourceInput(exc.errors(include_url=False)) from exc
    try:
        result = spec.detail_handler(parsed)
    except spec.not_found_errors as exc:
        # A provider declares these types; their text is written for the
        # provider, not for a caller, so answer with this module's own.
        raise ResourceNotFound(
            f"No {name!r} record matches the requested identifier."
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Resource {name!r} detail handler returned a non-object.")
    return result
