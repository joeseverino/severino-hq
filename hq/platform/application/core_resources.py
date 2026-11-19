"""The host's own read resources, as ``core_capabilities`` holds its capabilities."""

from typing import Annotated, Any, override

from django.db.models import Model
from pydantic import Field, create_model

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
from .connection_records import (
    ConnectionStandingRecord,
    ConnectionStandingRow,
    CredentialRecord,
    CredentialRow,
    Tailnet,
)
from .domains import queue_sources
from .integration_specs import ListRead, ResourceSpec, typed_keys
from .plugins import ATTENTION_ORDER
from .projection import MAX_PAGE_SIZE
from .resource_records import (
    ActionItem,
    ActionItemRow,
    ContainerRecord,
    ContainerRow,
    DomainRecord,
    DomainRow,
    EstateFigure,
    HopRow,
    MachineRecord,
    MachineRow,
    ManagedResourceItem,
    ManagedResourceRecord,
    ManagedResourceRow,
    Match,
    PathRecord,
    Problem,
    ReadingItem,
    ReadingRecord,
    ReadingRow,
    RelationshipsRecord,
    ServiceItem,
    ServiceRecord,
    ServiceRow,
    UpgradeRecord,
    UpgradeRow,
)
from .resource_refusals import INFRASTRUCTURE, READINGS
from .resources import TEXT_FILTER, BoundedQuery, EmptyQuery, TopQuery, one_of, several_of
from .search_contracts import SearchDefinition, search_lines
from .security import Capability


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


InfrastructureResourceQuery = create_model(
    "InfrastructureResourceQuery",
    __base__=BoundedQuery,
    kinds=(
        str | None,
        Field(
            default=None,
            max_length=2000,
            pattern=several_of(PROVIDERS),
            description="Several types to keep, comma-separated, each one the single type filter offers.",
        ),
    ),
    kind=(
        one_of(PROVIDERS) | None,
        Field(default=None, description="Only the records of this type."),
    ),
)


def action_item_query() -> type[BoundedQuery]:
    """The queue's narrowing: a status the queue holds, and a domain that raises
    items, as this composition declares them."""

    sources = queue_sources()
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


# Search's, which answers fewer unless asked for more.
type SearchLimit = Annotated[
    int, Field(ge=1, le=MAX_PAGE_SIZE, description=f"How many matches to return, 1 to {MAX_PAGE_SIZE}, best first.")
]


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
            INFRASTRUCTURE,
            "Records",
            "Everything HQ manages: what HQ expects and what is live.",
            Capability.READ,
            listing=ListRead(
                infrastructure.list_managed_resources,
                InfrastructureResourceQuery,
                ManagedResourceItem,
                fields=typed_keys(ManagedResourceRow),
            ),
            detail_handler=resource_context.get_managed_resource,
            identifier="key",
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
            record_type=ManagedResourceRecord,
        ),
        ResourceSpec(
            "services",
            "Services",
            "Every service name and the state of its DNS, proxy and certificate.",
            Capability.READ,
            listing=ListRead(service_list.list_services, EmptyQuery, ServiceItem, fields=typed_keys(ServiceRow)),
            detail_handler=service_list.get_service,
            identifier="hostname",
            web_route="control_plane:services",
            record_type=ServiceRecord,
        ),
        ResourceSpec(
            "estate",
            "Estate",
            "Machines online, services, domains, next renewals and connections needing attention.",
            Capability.READ,
            listing=ListRead(derived_reads.list_estate, EmptyQuery, EstateFigure),
            web_route="dashboard",
            pass_principal=True,
        ),
        ResourceSpec(
            "machines",
            "Machines",
            "Every machine anything reported, its roles, addresses and what runs on it.",
            Capability.READ,
            listing=ListRead(derived_reads.list_machines, BoundedQuery, MachineRecord, fields=typed_keys(MachineRow)),
            detail_handler=derived_reads.get_machine,
            identifier="name",
            web_route="control_plane:machines",
            record_type=MachineRecord,
        ),
        ResourceSpec(
            "containers",
            "Containers",
            "Every running container: its image and whether it is current and safe, its runtime, "
            "memory and CPU, and its posture.",
            Capability.READ,
            listing=ListRead(
                container_reads.list_containers, BoundedQuery, ContainerRecord, fields=typed_keys(ContainerRow)
            ),
            detail_handler=container_reads.get_container,
            identifier="address",
            web_route="control_plane:containers",
            record_type=ContainerRecord,
        ),
        ResourceSpec(
            "upgrades",
            "Upgrades",
            "Every running container with a newer release: target digest, fixes, data, verification, "
            "blockers and steps. Read-only.",
            Capability.READ,
            listing=ListRead(container_reads.list_upgrades, BoundedQuery, UpgradeRecord, fields=typed_keys(UpgradeRow)),
            detail_handler=container_reads.get_upgrade,
            identifier="address",
            web_route="control_plane:containers",
            record_type=UpgradeRecord,
        ),
        ResourceSpec(
            "domains",
            "Domains",
            "Every domain HQ manages or has read, its services and registration.",
            Capability.READ,
            listing=ListRead(derived_reads.list_domains, BoundedQuery, DomainRow),
            detail_handler=derived_reads.get_domain,
            identifier="name",
            web_route="zones:index",
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
            "HQ itself: its release and branch, machine and container, background jobs, "
            "what it reads and open problems.",
            Capability.READ,
            listing=ListRead(hq_context.read_hq, EmptyQuery, Problem),
            web_route="hq_self",
            pass_principal=True,
        ),
        ResourceSpec(
            READINGS,
            "Readings",
            "Each type of reading, when it was last read, and the records it holds.",
            Capability.READ,
            listing=ListRead(derived_reads.list_readings, ReadingQuery, ReadingItem, fields=typed_keys(ReadingRow)),
            detail_handler=derived_reads.get_reading,
            identifier="kind",
            web_route="control_plane:connections",
            record_type=ReadingRecord,
        ),
        ResourceSpec(
            "credentials",
            "Credential access",
            "What each connection's credential can read, and what it would need to read more.",
            Capability.READ,
            listing=ListRead(
                derived_reads.list_credentials, EmptyQuery, CredentialRecord, fields=typed_keys(CredentialRow)
            ),
            detail_handler=derived_reads.get_credential,
            identifier="provider",
            web_route="control_plane:connections",
            record_type=CredentialRecord,
        ),
        ResourceSpec(
            "tailnet",
            "Tailnet",
            "The tailnet policy: settings, grants, shell rules, groups, tags and their machines, "
            "services, app connectors, tests, problems and read failures.",
            Capability.READ,
            listing=ListRead(tailnet_context.get_tailnet, EmptyQuery, Tailnet),
            web_route="control_plane:tailnet",
            pass_principal=True,
        ),
        ResourceSpec(
            "connections",
            "Connection status",
            "Each connection with what its credential sees, how fresh each reading is, what "
            "was refused and the fix. It also gives the counts, the security posture and HQ's path.",
            Capability.READ,
            listing=ListRead(
                connection_context.list_connection_standing,
                EmptyQuery,
                ConnectionStandingRecord,
                fields=typed_keys(ConnectionStandingRow),
            ),
            detail_handler=connection_context.get_connection_standing,
            identifier="connection_ref",
            web_route="control_plane:connections",
            pass_principal=True,
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
            listing=ListRead(
                derived_reads.list_action_items, action_item_query(), ActionItem, fields=typed_keys(ActionItemRow)
            ),
            web_route="action_items",
            pass_principal=True,
        ),
    )


def connections() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "request.path",
            "Request path",
            "How the calling request reached HQ, hop by hop, and the request standard's checks decided at each.",
            Capability.READ,
            listing=ListRead(derived_reads.list_request_path, EmptyQuery, HopRow),
            web_route="connection",
            pass_principal=True,
        ),
    )


def search() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "search",
            "Search",
            "Machines, services and domains, then records, matching a query.",
            Capability.READ,
            listing=ListRead(derived_reads.search, SearchQuery, Match),
            web_route="search",
            pass_principal=True,
        ),
    )
