"""What HQ itself can be asked to do: its own capability declarations.

One entry per command, each naming its effect, the authority it needs and the
service that carries it out. A record domain's create, update and delete are
not here: ``application.records`` derives them from its declaration.
``application.integrations`` joins these with what each host domain and
extension declares; ``application.capabilities`` runs them.
"""

from hq.domains.control_plane.provider_adapters.tls import CERTIFICATE_KIND

from .cadence import ControllerSweepCommand, request_controller_sweep
from .documentation import (
    DocumentationSyncCommand,
    execute_documentation_sync,
)
from .domains import records_of
from .infrastructure import ManagedResourceCommand, save_managed_resource
from .integration_specs import CapabilitySpec
from .lookup import (
    AddressCommand,
    NameCommand,
    look_up_address,
    look_up_name,
)
from .policy_fixes import request_empty_groups_removal
from .projects import (
    ProjectRefreshCommand,
    execute_project_refresh,
)
from .registry_import import REQUIRED_CAPABILITIES as IMPORT_CAPABILITIES, HQImportCommand, execute_hq_import
from .resource_operations import (
    OperationCommand,
    accept_observed,
    request_certificate_renewal,
    request_reach_allow,
    request_reconcile,
    request_removal,
    request_route_approval,
)
from .security import Capability
from .sync import HQSyncCommand, execute_hq_sync
from .tailnet import POLICY_KIND as TAILNET_POLICY_KIND, TAILNET_KIND

CORE_CAPABILITY_SPECS = (
    CapabilitySpec(
        "hq.sync",
        "Bring HQ's list of docs up to date from the vault.",
        "remote_write",
        # Documentation authority alone, so an account allowed to sync docs
        # needs no control-plane authority.
        Capability.SYNC_DOCUMENTATION,
        HQSyncCommand,
        execute_hq_sync,
        subject_resource="documentation",
        label="Sync the vault",
    ),
    CapabilitySpec(
        "hq.import",
        "Import projects and assets from one file. Nothing is saved unless every record in it is valid.",
        "remote_write",
        # The capabilities of the upserts it runs, both of them.
        IMPORT_CAPABILITIES,
        HQImportCommand,
        execute_hq_import,
        subject_resource="projects",
        execution_notes=(
            "Validate every record before writing any.",
            "Upsert each record through project.upsert or asset.upsert, in one transaction.",
            "Keep a stored derived field that differs, and report it.",
            "Record one audit event per record and one for the import.",
        ),
        label="Import projects and assets",
    ),
    CapabilitySpec(
        "project.refresh",
        "Read a project's latest GitHub activity and published pages.",
        "remote_write",
        records_of("projects").write,
        ProjectRefreshCommand,
        execute_project_refresh,
        "slug",
        "projects",
        target_label="Project",
        target_help="The project to refresh.",
        execution_notes=(
            "Start the refresh as a job and answer at once with the job's id; the work runs outside the request.",
            "Where the GitHub App reads the repository, ask the controller to read that "
            "connection now, through infrastructure.controller.refresh.",
            "For a repository the App does not read, read its last push from GitHub's "
            "public API, which needs no credential.",
            "Save when it was last pushed to, and record who asked.",
        ),
        label="Refresh a project",
    ),
    CapabilitySpec(
        "documentation.sync",
        "Bring HQ's list of docs up to date from a vault export.",
        "remote_write",
        Capability.SYNC_DOCUMENTATION,
        DocumentationSyncCommand,
        execute_documentation_sync,
        subject_resource="documentation",
        label="Sync documentation",
    ),
    CapabilitySpec(
        "infrastructure.resource.create",
        "Add something for HQ to manage.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        ManagedResourceCommand,
        save_managed_resource,
        subject_resource="infrastructure.resources",
        label="Add a record",
    ),
    CapabilitySpec(
        "infrastructure.resource.update",
        "Change HQ's copy of a record. HQ then applies it to the live system.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        ManagedResourceCommand,
        save_managed_resource,
        "key",
        "infrastructure.resources",
        target_label="Record",
        target_help="The record to change.",
        target_initial_fields=("key", "kind", "spec", "enabled"),
        label="Change what HQ expects",
    ),
    CapabilitySpec(
        "infrastructure.resource.accept_observed",
        "Copy what is live into HQ's copy, keeping a change made outside HQ.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        accept_observed,
        "key",
        "infrastructure.resources",
        target_label="Record",
        target_help="The record whose live version to keep.",
        label="Keep the live version",
    ),
    CapabilitySpec(
        "infrastructure.reconcile",
        "Apply HQ's copy of one record to the live system.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_reconcile,
        "key",
        "infrastructure.resources",
        target_label="Record",
        target_help="The record to apply.",
        label="Apply HQ's settings",
    ),
    CapabilitySpec(
        "infrastructure.controller.refresh",
        "Ask the controller to read everything again now.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        ControllerSweepCommand,
        request_controller_sweep,
        execution_notes=(
            "Mark HQ as in use, so readings are taken more often.",
            "Wake the controller. HQ's web process holds no credential for any service.",
            "The controller reads what HQ says is due, with its own credentials.",
        ),
        label="Read everything now",
    ),
    CapabilitySpec(
        "infrastructure.resource.remove",
        "Remove the live thing this record describes, then remove the record from HQ.",
        "destructive",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_removal,
        "key",
        "infrastructure.resources",
        target_label="Record",
        target_help="The record to remove.",
        label="Remove a record",
    ),
    CapabilitySpec(
        "tailnet.routes.approve",
        "Approve the routes a tailnet device already offers.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_route_approval,
        "key",
        "infrastructure.resources",
        target_label="Device",
        target_help="The tailnet device whose routes to approve.",
        target_query=(("kind", TAILNET_KIND),),
        execution_notes=(
            "Read what the device currently advertises and what is already approved.",
            "Queue one approval for the controller; the API call runs outside this request.",
            "Approve exactly the advertised set, so no route this was not about is withdrawn.",
        ),
        label="Approve subnet routes",
    ),
    CapabilitySpec(
        "tailnet.reach.allow",
        "Allow a connection Tailscale blocks that a reading needs.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_reach_allow,
        "key",
        "infrastructure.resources",
        target_label="Certificate",
        target_help="The certificate used by a machine the controller could not reach.",
        # Scoped to what it acts on, not to what it changes. The amendment
        # lands on the tailnet policy, but the thing an operator selects is the
        # certificate whose consumer went unread: the same split that keeps
        # `certificate.renew` off the tailnet ability.
        target_query=(("kind", CERTIFICATE_KIND),),
        execution_notes=(
            "Read the addresses and ports the last reading could not reach.",
            "Ask the policy whether it refuses each one, and keep only those it does.",
            "Change the Tailscale policy. A person approves the change before anything reaches the tailnet.",
        ),
        label="Allow a blocked Tailscale connection",
    ),
    CapabilitySpec(
        "tailnet.policy.remove_empty_groups",
        "Remove the groups with no members that the tailnet policy still grants.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_empty_groups_removal,
        "key",
        "infrastructure.resources",
        target_label="Tailscale policy",
        target_help="The Tailscale policy to change.",
        target_query=(("kind", TAILNET_POLICY_KIND),),
        execution_notes=(
            "Read HQ's copy of the policy and find the groups with no members that a rule names.",
            "Refuse when such a group is named anywhere else, since removing it would change what the policy means.",
            "Strike them from their rules, dropping a rule left admitting nobody, and "
            "propose the changed policy, which a person approves.",
        ),
        label="Remove empty groups",
    ),
    CapabilitySpec(
        "certificate.renew",
        "Ask for a certificate to be renewed, where its renewal rules allow it.",
        "infrastructure_change",
        Capability.REQUEST_CERTIFICATE_RENEWAL,
        OperationCommand,
        request_certificate_renewal,
        "key",
        "infrastructure.resources",
        target_label="Certificate",
        target_help="The certificate to renew.",
        target_query=(("kind", CERTIFICATE_KIND),),
        execution_notes=(
            "Read the certificate's settings and check its renewal rules.",
            "Queue one renewal for the controller, which does the work outside this request.",
            "Return what was queued and why, recorded under whoever asked.",
        ),
        label="Renew certificate",
    ),
    # Capabilities that read something HQ does not hold. Both are `read`, so
    # neither takes an idempotency key and neither writes: asking a
    # registry the same question twice is the same question twice.
    CapabilitySpec(
        "lookup.name",
        "Ask a public resolver what the internet returns for a hostname.",
        "read",
        Capability.LOOK_UP_PUBLIC_RECORDS,
        NameCommand,
        look_up_name,
        execution_notes=(
            "Validate the hostname before anything leaves this machine.",
            "Ask one resolver outside this network, so internal rewrites cannot "
            "answer a question about the public internet.",
            "Return the records as the resolver gave them, with no TTL: this "
            "resolver reports a constant, which is not a measurement.",
        ),
        label="Look up a name",
    ),
    CapabilitySpec(
        "lookup.address",
        "Ask what name and which allocation a public address belongs to.",
        "read",
        Capability.LOOK_UP_PUBLIC_RECORDS,
        AddressCommand,
        look_up_address,
        execution_notes=(
            "Refuse a private address locally; nothing outside can describe it, "
            "and asking would disclose it for no answer.",
            "Read reverse DNS, which the address holder publishes and which usually carries a brand name.",
            "Read the RDAP allocation, which the registry publishes and which "
            "carries the company. Either registry may fail without the other.",
        ),
        label="Look up an address",
    ),
)
