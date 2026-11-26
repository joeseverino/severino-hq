"""The records HQ manages: their commands and their readable resource."""

from typing import Any, override

from django.db.models import Model
from pydantic import Field, create_model

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.domains.control_plane.provider_adapters.tls import CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND
from hq.domains.control_plane.providers import PROVIDERS, spec_withheld

from . import (
    app_update_approval,
    app_update_open,
    deployment_review,
    infrastructure,
    resource_context,
    resource_operations as operations,
    services,
)
from .adoption import ADOPT_SERVICE, AdoptServiceCommand, adopt_service
from .cadence import CONTROLLER_REFRESH
from .certificates import UPLOAD, UploadCertificateCommand, store_certificate
from .contracts import Effect
from .integration_specs import CapabilitySpec, ListRead, ResourceSpec, typed_keys
from .labels import adding
from .managed_records import ManagedResourceListed, ManagedResourceRecord, ManagedResourceRow
from .policy_fixes import request_empty_groups_removal
from .records import EDIT, KEPT
from .resource_capabilities import MACHINE_DETAILS
from .resource_refusals import INFRASTRUCTURE
from .resources import BoundedQuery, one_of, several_of
from .search_contracts import SearchDefinition, search_lines
from .security import Capability
from .tailnet import POLICY_KIND as TAILNET_POLICY_KIND, TAILNET_KIND


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            "infrastructure.resource.create",
            "Add something for HQ to manage.",
            Effect.REMOTE_WRITE,
            Capability.MANAGE_INFRASTRUCTURE,
            infrastructure.ManagedResourceCommand,
            infrastructure.save_managed_resource,
            subject_resource="infrastructure",
            label=adding("record"),
            # A machine page's own create: details for a machine the tailnet's
            # declaration names, or a machine declared from nothing.
            variants=((MACHINE_DETAILS, "Add machine details"), (MACHINE_KIND, "Add this machine to HQ")),
        ),
        CapabilitySpec(
            "infrastructure.resource.update",
            f"Change HQ's copy of a record. HQ then applies it to the live system. {KEPT}",
            Effect.REMOTE_WRITE,
            Capability.MANAGE_INFRASTRUCTURE,
            infrastructure.ManagedResourceCommand,
            infrastructure.save_managed_resource,
            "key",
            "infrastructure",
            target_label="Record",
            target_help="The record to change.",
            target_initial_fields=("key", "kind", "spec", "enabled"),
            label=f"{EDIT} a record",
            verb=EDIT,
            wants="change the expected settings of {target}",
        ),
        CapabilitySpec(
            ADOPT_SERVICE,
            "Manage every record HQ found live behind one hostname, or none of them.",
            Effect.REMOTE_WRITE,
            Capability.MANAGE_INFRASTRUCTURE,
            AdoptServiceCommand,
            adopt_service,
            subject_resource="infrastructure",
            execution_notes=(
                "Find the hostname's records that HQ does not manage yet.",
                "Adopt each one through a connection that manages, in one transaction.",
            ),
            label="Manage a service in HQ",
            verb="Manage in HQ",
        ),
        CapabilitySpec(
            "infrastructure.resource.accept_observed",
            "Copy what is live into HQ's copy, keeping a change made outside HQ.",
            Effect.REMOTE_WRITE,
            Capability.MANAGE_INFRASTRUCTURE,
            operations.OperationCommand,
            operations.accept_observed,
            "key",
            "infrastructure",
            target_label="Record",
            target_help="The record whose live version to keep.",
            label="Keep the live version",
            wants="keep the live version of {target}",
        ),
        CapabilitySpec(
            operations.ACTION_CAPABILITY[OperationRequest.Action.RECONCILE],
            "Apply HQ's copy of one record to the live system.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            operations.OperationCommand,
            operations.request_reconcile,
            "key",
            "infrastructure",
            target_label="Record",
            target_help="The record to apply.",
            label=OperationRequest.Action.RECONCILE.label,
            # Applying a record that only reports on itself reads it again.
            variants=((operations.READS_AGAIN, CONTROLLER_REFRESH.words()),),
            wants="apply HQ's config to {target}",
        ),
        CapabilitySpec(
            operations.ACTION_CAPABILITY[OperationRequest.Action.DELETE],
            "Remove what this record describes from where it runs, then the record from HQ.",
            Effect.DESTRUCTIVE,
            Capability.MANAGE_INFRASTRUCTURE,
            operations.OperationCommand,
            operations.request_removal,
            "key",
            "infrastructure",
            target_label="Record",
            target_help="The record to remove.",
            label="Remove a record",
            verb=OperationRequest.Action.DELETE.label,
            # A record HQ only watches is forgotten, and nothing live is touched.
            variants=(("forget", "Stop tracking"),),
        ),
        *(
            CapabilitySpec(
                operations.ACTION_CAPABILITY[action],
                summary,
                effect,
                Capability.MANAGE_INFRASTRUCTURE,
                operations.OperationCommand,
                handler,
                "key",
                "infrastructure",
                target_label="Container",
                target_help="The container to act on.",
                target_query=(("kind", CONTAINER_KIND),),
                execution_notes=(
                    "Refuse a container switched off in HQ, or one Portainer cannot do this to.",
                    "Queue it once for the controller, which does it outside this request.",
                ),
                label=f"{action.label} a container",
                verb=action.label,
            )
            for action, handler, summary, effect in (
                (
                    OperationRequest.Action.RESTART,
                    operations.request_restart,
                    "Restart the container through Portainer.",
                    Effect.INFRASTRUCTURE_CHANGE,
                ),
                (
                    OperationRequest.Action.START,
                    operations.request_start,
                    "Start the container through Portainer.",
                    Effect.INFRASTRUCTURE_CHANGE,
                ),
                # A stop takes the thing away from whoever uses it until someone
                # starts it again: an agent's waits for a person by default, and a
                # person's is confirmed on a page of its own.
                (
                    OperationRequest.Action.STOP,
                    operations.request_stop,
                    "Stop the container through Portainer; it stays down until it is started.",
                    Effect.DESTRUCTIVE,
                ),
            )
        ),
        CapabilitySpec(
            operations.ACTION_CAPABILITY[OperationRequest.Action.APPROVE_ROUTES],
            "Approve the routes a tailnet device already offers.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            operations.OperationCommand,
            operations.request_route_approval,
            "key",
            "infrastructure",
            target_label="Device",
            target_help="The tailnet device whose routes to approve.",
            target_query=(("kind", TAILNET_KIND),),
            execution_notes=(
                "Read what the device currently advertises and what is already approved.",
                "Queue one approval for the controller; the API call runs outside this request.",
                "Approve exactly the advertised set, so no other route is withdrawn.",
            ),
            label="Approve subnet routes",
            verb=OperationRequest.Action.APPROVE_ROUTES.label,
            wants="approve the subnet routes {target} offers",
        ),
        CapabilitySpec(
            "tailnet.reach.allow",
            "Allow a connection Tailscale blocks that a reading needs.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            operations.OperationCommand,
            operations.request_reach_allow,
            "key",
            "infrastructure",
            target_label="Certificate",
            target_help="The certificate used by a machine HQ could not reach.",
            # Scoped to what it acts on, not to what it changes. The amendment
            # lands on the tailnet policy, but the thing an operator selects is the
            # certificate whose consumer went unread: the same split that keeps
            # `certificate.renew` off the tailnet ability.
            target_query=(("kind", CERTIFICATE_KIND),),
            execution_notes=(
                "Read the addresses and ports the last reading could not reach.",
                "Ask the policy whether it refuses each one, and keep only those it does.",
                "Propose the change to the Tailscale policy. Nothing reaches the tailnet until an operator approves it.",
            ),
            label="Allow a blocked Tailscale connection",
            verb="Allow in policy",
            wants="allow the connections Tailscale blocks for {target}",
        ),
        CapabilitySpec(
            "tailnet.policy.remove_empty_groups",
            "Remove the groups with no members that the tailnet policy still grants.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            operations.OperationCommand,
            request_empty_groups_removal,
            "key",
            "infrastructure",
            target_label="Tailscale policy",
            target_help="The Tailscale policy to change.",
            target_query=(("kind", TAILNET_POLICY_KIND),),
            execution_notes=(
                "Read HQ's copy of the policy and find the groups with no members that a rule names.",
                "Refuse when such a group is named anywhere else, since removing it would change what the policy means.",
                (
                    "Strike them from their rules, drop any rule left empty, and propose the "
                    "changed policy for an operator to approve."
                ),
            ),
            label="Remove empty groups",
            wants="remove the empty groups from {target}",
        ),
        CapabilitySpec(
            operations.ACTION_CAPABILITY[OperationRequest.Action.RENEW],
            "Ask for a certificate to be renewed, where its renewal rules allow it.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.REQUEST_CERTIFICATE_RENEWAL,
            operations.OperationCommand,
            operations.request_certificate_renewal,
            "key",
            "infrastructure",
            target_label="Certificate",
            target_help="The certificate to renew.",
            target_query=(("kind", CERTIFICATE_KIND),),
            execution_notes=(
                "Read the certificate's settings and check its renewal rules.",
                "Queue one renewal for the controller, which does the work outside this request.",
                "Return what was queued and why, recorded under the caller's name.",
            ),
            label="Renew a certificate",
            verb=OperationRequest.Action.RENEW.label,
        ),
        CapabilitySpec(
            UPLOAD,
            "Hold a certificate and key you generated elsewhere, for HQ to install.",
            Effect.INFRASTRUCTURE_CHANGE,
            # Held only by an operator in the browser or at the host: a private key
            # never arrives with a token (security.HELD_ONLY_AT).
            Capability.UPLOAD_CERTIFICATE,
            UploadCertificateCommand,
            store_certificate,
            "key",
            "infrastructure",
            target_label="Certificate",
            target_help="The uploaded certificate record to hold it for.",
            target_query=(("kind", UPLOADED_CERTIFICATE_KIND),),
            execution_notes=(
                "Refuse a certificate that has expired or a key that does not match it.",
                "Seal the key to the controller, refusing when HQ has no sealing key.",
                "Record the names it covers and move the record on, so it is installed.",
            ),
            label="Upload a certificate",
            variants=(("replace", "Replace certificate"),),
        ),
        *app_update_approval.capabilities(),
        *app_update_open.capabilities(),
        *deployment_review.capabilities(),
    )


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

SEARCH = ResourceSearchDefinition.of(
    ManagedResource,
    "key",
    ("key", "kind", "spec", "status", "conditions"),
    title_field="key",
    badge_field="kind_label",
    snippet_field="search_summary",
)
FIELDS = typed_keys(ManagedResourceRow)


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            INFRASTRUCTURE,
            "Records",
            "Everything HQ manages: what HQ expects and what is live.",
            Capability.READ,
            listing=ListRead(
                resource_context.list_managed_resources,
                InfrastructureResourceQuery,
                ManagedResourceListed,
                fields=FIELDS,
            ),
            detail_handler=resource_context.get_managed_resource,
            identifier="key",
            search=SEARCH,
            web_route="control_plane:list",
            record_type=ManagedResourceRecord,
        ),
    )
