"""What HQ itself can be asked to do: its own capability declarations.

One entry per command, each naming its effect, the authority it needs and the
service that carries it out. A record domain's create, update and delete are
not here: ``application.records`` derives them from its declaration.
``application.integrations`` joins these with what each host domain and
extension declares; ``application.capabilities`` runs them.
"""

from hq.domains.control_plane.models import OperationRequest
from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.domains.control_plane.provider_adapters.tls import CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND

from . import action_items, pins
from .adoption import ADOPT, ADOPT_SERVICE, AdoptCommand, AdoptServiceCommand, adopt, adopt_service
from .agent_access import (
    REMOVE_AGENT,
    SET_AGENT_ACCESS,
    SET_AGENT_BLOCK,
    AgentAccessCommand,
    AgentBlockCommand,
    AgentRemoveCommand,
    agents_paused,
    blocked,
    remove_agent,
    set_agent_blocked,
    set_agents_paused,
)
from .agent_devices import (
    APPROVE_AGENT_DEVICE,
    BLOCK_AGENT_DEVICE,
    AgentDeviceApprovalCommand,
    AgentDeviceBlockCommand,
    device_approved,
    device_blocked,
    set_device_approved,
    set_device_blocked,
)
from .appearance import SET_THEME, ThemeCommand, set_theme
from .approvals import (
    APPROVE,
    DESTRUCTIVE_EFFECT,
    REJECT,
    WITHDRAW,
    ApproveCommand,
    RejectCommand,
    WithdrawCommand,
    answered,
    approve,
    reject,
    withdraw,
)
from .cadence import ControllerSweepCommand, request_controller_sweep
from .capability_policy import SET_POLICY, PolicyCommand, apply_changes
from .certificates import UPLOAD, UploadCertificateCommand, store_certificate
from .dependency_updates import RUN_NOW as DEPENDENCY_RUN_NOW, DependencyRunCommand, request_run
from .documentation import (
    SYNC as DOCUMENTATION_SYNC,
    DocumentationSyncCommand,
    execute_documentation_sync,
)
from .domains import records_of
from .github_profile import READ_NOW as PROFILE_READ_NOW, ProfileReadCommand, request_read as request_profile_read
from .glance import (
    DASHBOARD_MACHINE,
    DASHBOARD_REFRESH,
    DASHBOARD_REFRESH_STALE,
    DASHBOARD_SETTINGS,
    DashboardMachineCommand,
    DashboardRefreshCommand,
    DashboardSettingsCommand,
    dashboard_machine_selected,
    request_dashboard_refresh,
    request_stale_panel_refresh,
    save_dashboard_settings,
    select_dashboard_machine,
)
from .infrastructure import ManagedResourceCommand, save_managed_resource
from .integration_specs import ON_ITS_PAGE, CapabilitySpec
from .labels import adding
from .lookup import (
    LOOK_UP_ADDRESS,
    LOOK_UP_NAME,
    AddressCommand,
    NameCommand,
    look_up_address,
    look_up_name,
)
from .own_devices import SET_OWN_DEVICE, OwnDeviceCommand, set_own_device
from .policy_fixes import request_empty_groups_removal
from .projects import (
    DEPENDENCY_UPDATES,
    DependencyUpdatesCommand,
    set_dependency_updates,
)
from .receipts import (
    REPLACE as RECEIPT_REPLACE,
    UPLOAD as RECEIPT_UPLOAD,
    ReceiptUploadCommand,
    replace_receipt,
    upload_receipt,
)
from .records import EDIT, KEPT
from .registry_import import REQUIRED_CAPABILITIES as IMPORT_CAPABILITIES, HQImportCommand, execute_hq_import
from .resource_operations import (
    ACTION_CAPABILITY,
    OperationCommand,
    accept_observed,
    request_certificate_renewal,
    request_reach_allow,
    request_reconcile,
    request_removal,
    request_restart,
    request_route_approval,
    request_start,
    request_stop,
)
from .security import Capability
from .sessions import SIGN_OUT, SignOutCommand, sign_out
from .sync import HQSyncCommand, execute_hq_sync
from .tailnet import POLICY_KIND as TAILNET_POLICY_KIND, TAILNET_KIND
from .visit_refresh import VISIT_REFRESH, VisitRefreshCommand, request_visit_refresh
from .zones import ADOPT_ZONE, AdoptZoneCommand, adopt_zone

# Named, so prose that tells a person which button to press reads its words.
CONTROLLER_REFRESH = CapabilitySpec(
    "infrastructure.controller.refresh",
    "Ask HQ to read everything again now.",
    "infrastructure_change",
    Capability.MANAGE_INFRASTRUCTURE,
    ControllerSweepCommand,
    request_controller_sweep,
    execution_notes=(
        "Mark HQ as in use, so readings are taken more often.",
        "Wake the controller. HQ's web process holds no credential for any service.",
        "The controller reads what HQ says is due, with its own credentials.",
    ),
    label="Refresh HQ's readings",
    verb="Refresh",
    variants=(("all", "Refresh all"),),
)

_RECEIPT_FILE_NOTES = (
    "Refuse base64 text longer than the size limit allows, before decoding it.",
    "Refuse a file over the limit, or of a type a receipt cannot be.",
    "Record the file's name, size and SHA-256 digest; its bytes are kept only as the receipt's file.",
)

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
        label="Update docs from the vault",
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
        DEPENDENCY_UPDATES,
        "Turn HQ's dependency update pull requests on or off for a project's repository, from the next run.",
        "remote_write",
        records_of("projects").write,
        DependencyUpdatesCommand,
        set_dependency_updates,
        "slug",
        "projects",
        target_label="Project",
        target_help="The project whose repository to update.",
        execution_notes=(
            "Refuse a project that names no repository.",
            "Turn updates on only while the repository passes its vulnerability-alerts check.",
            "Record the switch and who set it; the next update run reads it.",
        ),
        target_initial_fields=("dependency_updates",),
        label="Turn dependency updates on or off",
        variants=(("on", "Turn on updates"), ("off", "Turn off updates")),
        wants="turn dependency updates on or off for {target}",
    ),
    CapabilitySpec(
        DEPENDENCY_RUN_NOW,
        "Update dependencies in every opted-in repository.",
        # The controller's run opens pull requests on GitHub.
        "infrastructure_change",
        # The ask is a controller read, which needs infrastructure as well.
        (records_of("projects").write, Capability.MANAGE_INFRASTRUCTURE),
        DependencyRunCommand,
        request_run,
        subject_resource="projects",
        execution_notes=(
            "Refuse when no project is opted in.",
            "Ask the controller for a run and wake it; it hands one token per account to the host.",
            "The run's outcome comes back as the github.dependency_run reading.",
        ),
        label="Update dependencies",
    ),
    CapabilitySpec(
        DOCUMENTATION_SYNC,
        "Bring HQ's list of docs up to date from a vault export.",
        "remote_write",
        Capability.SYNC_DOCUMENTATION,
        DocumentationSyncCommand,
        execute_documentation_sync,
        subject_resource="documentation",
        label="Update docs from an export",
    ),
    CapabilitySpec(
        "infrastructure.resource.create",
        "Add something for HQ to manage.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        ManagedResourceCommand,
        save_managed_resource,
        subject_resource="infrastructure",
        label=adding("record"),
    ),
    CapabilitySpec(
        "infrastructure.resource.update",
        f"Change HQ's copy of a record. HQ then applies it to the live system. {KEPT}",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        ManagedResourceCommand,
        save_managed_resource,
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
        ADOPT,
        "Manage a record HQ found live, starting from exactly what is live.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        AdoptCommand,
        adopt,
        subject_resource="infrastructure",
        execution_notes=(
            "Read the record again as HQ last found it live, never from the request.",
            "Write HQ's copy equal to it, so applying it changes nothing.",
        ),
        label="Manage a record in HQ",
        verb="Manage in HQ",
        wants="manage a record HQ found live",
        reached_from="control_plane:services",
    ),
    CapabilitySpec(
        ADOPT_SERVICE,
        "Manage every record HQ found live behind one hostname, or none of them.",
        "remote_write",
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
        ADOPT_ZONE,
        "Manage a domain HQ found live, and every record in it, exactly as they are.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        AdoptZoneCommand,
        adopt_zone,
        subject_resource="infrastructure",
        execution_notes=(
            "Adopt the domain as HQ last found it live.",
            "Adopt every record in it HQ can, in one transaction; the domain stands if none can be.",
        ),
        label="Manage a domain in HQ",
        reached_from="zones:index",
    ),
    CapabilitySpec(
        "infrastructure.resource.accept_observed",
        "Copy what is live into HQ's copy, keeping a change made outside HQ.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        accept_observed,
        "key",
        "infrastructure",
        target_label="Record",
        target_help="The record whose live version to keep.",
        label="Keep the live version",
        wants="keep the live version of {target}",
    ),
    CapabilitySpec(
        ACTION_CAPABILITY[OperationRequest.Action.RECONCILE],
        "Apply HQ's copy of one record to the live system.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_reconcile,
        "key",
        "infrastructure",
        target_label="Record",
        target_help="The record to apply.",
        label=OperationRequest.Action.RECONCILE.label,
        wants="apply HQ's config to {target}",
    ),
    CapabilitySpec(
        SIGN_OUT,
        "Sign a user out of every HQ session when Pocket ID signs them out.",
        "remote_write",
        Capability.END_SESSIONS,
        SignOutCommand,
        sign_out,
        execution_notes=(
            "Find the user who signed in as Pocket ID's subject; if none did, there is nothing to do.",
            "End every live session of theirs, leaving the account and any local password alone.",
        ),
        label="Sign a user out",
    ),
    CapabilitySpec(
        ACTION_CAPABILITY[OperationRequest.Action.DELETE],
        "Remove what this record describes from where it runs, then the record from HQ.",
        DESTRUCTIVE_EFFECT,
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_removal,
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
            ACTION_CAPABILITY[action],
            summary,
            effect,
            Capability.MANAGE_INFRASTRUCTURE,
            OperationCommand,
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
                request_restart,
                "Restart the container through Portainer.",
                "infrastructure_change",
            ),
            (
                OperationRequest.Action.START,
                request_start,
                "Start the container through Portainer.",
                "infrastructure_change",
            ),
            # A stop takes the thing away from whoever uses it until someone
            # starts it again: an agent's waits for a person by default, and a
            # person's is confirmed on a page of its own.
            (
                OperationRequest.Action.STOP,
                request_stop,
                "Stop the container through Portainer; it stays down until it is started.",
                DESTRUCTIVE_EFFECT,
            ),
        )
    ),
    CapabilitySpec(
        ACTION_CAPABILITY[OperationRequest.Action.APPROVE_ROUTES],
        "Approve the routes a tailnet device already offers.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_route_approval,
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
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_reach_allow,
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
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
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
        ACTION_CAPABILITY[OperationRequest.Action.RENEW],
        "Ask for a certificate to be renewed, where its renewal rules allow it.",
        "infrastructure_change",
        Capability.REQUEST_CERTIFICATE_RENEWAL,
        OperationCommand,
        request_certificate_renewal,
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
        "infrastructure_change",
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
    ),
    # A receipt's file arrives only through an operator in the browser or at
    # the host (security.HELD_ONLY_AT), though a token may read the schema.
    CapabilitySpec(
        RECEIPT_UPLOAD,
        "Keep a receipt's file, out of reach of any public address, with its details.",
        "remote_write",
        (records_of("receipts").write, Capability.UPLOAD_RECEIPT),
        ReceiptUploadCommand,
        upload_receipt,
        subject_resource="receipts",
        execution_notes=_RECEIPT_FILE_NOTES,
        label=adding("receipt"),
    ),
    CapabilitySpec(
        RECEIPT_REPLACE,
        f"Replace a receipt's file, and change its details with it. {KEPT}",
        "remote_write",
        (records_of("receipts").write, Capability.UPLOAD_RECEIPT),
        ReceiptUploadCommand,
        replace_receipt,
        "integer",
        "receipts",
        target_label="Receipt",
        target_help="The receipt whose file to replace.",
        execution_notes=_RECEIPT_FILE_NOTES,
        label="Replace a receipt's file",
    ),
    # The dashboard everyone's pages share, arranged by whoever holds
    # set_preferences: an operator's, since a viewer holds only reads.
    CapabilitySpec(
        DASHBOARD_MACHINE,
        "Show a machine's telemetry on the dashboard, or take it off.",
        "remote_write",
        Capability.SET_PREFERENCES,
        DashboardMachineCommand,
        select_dashboard_machine,
        "key",
        "infrastructure",
        target_label="Machine",
        target_help="The machine whose telemetry to show.",
        target_query=(("kind", MACHINE_KIND),),
        execution_notes=("Refuse a record that is not an enabled machine.",),
        label="Show a machine on the dashboard",
        verb="Show on dashboard",
        wants="show {target} on the dashboard",
        variants=(("on", "Show on dashboard"), ("off", "Remove from dashboard")),
        state=dashboard_machine_selected,
    ),
    # A person's decision on a change a credential asked for. Held only in the
    # browser (security.HELD_ONLY_AT), so no credential decides one.
    CapabilitySpec(
        APPROVE,
        "Agree to a change an agent asked for, and apply exactly what it asked.",
        "remote_write",
        Capability.DECIDE_APPROVALS,
        ApproveCommand,
        answered(approve),
        execution_notes=(
            "Refuse a request already decided or expired, and one you asked for yourself.",
            "Refuse when what it changes moved on after it was asked for.",
            "Run the held call once, recorded under both the asker and the approver.",
        ),
        label="Approve a held change",
        reached_from="action_items",
    ),
    CapabilitySpec(
        REJECT,
        "Refuse a change an agent asked for, so its request has an answer.",
        "remote_write",
        Capability.DECIDE_APPROVALS,
        RejectCommand,
        answered(reject),
        execution_notes=("Record the refusal, who made it and the note, and apply nothing.",),
        label="Reject a held change",
        reached_from="action_items",
    ),
    # The asker's own way out of a request it no longer wants. Its own
    # request only, so any caller that asked may: a person decides nothing here.
    CapabilitySpec(
        WITHDRAW,
        "Take back a change you asked for that still waits for approval, so no operator is asked to decide it. "
        "Send the `approval.id` its held answer carried.",
        "remote_write",
        Capability.READ,
        WithdrawCommand,
        answered(withdraw),
        execution_notes=(
            "Find the request among the ones you asked for, and refuse any other.",
            "Refuse a request already decided or expired.",
            "Mark it withdrawn, apply nothing, and stop counting it against how many you may hold.",
        ),
        label="Withdraw a held change",
        verb="Withdraw",
        subject_resource="approvals",
    ),
    # Who may act as an agent, decided by a person (security.HELD_ONLY_AT), so
    # no credential widens or frees itself.
    CapabilitySpec(
        SET_AGENT_ACCESS,
        "Pause every agent, on MCP and the machine API, or let them back in.",
        "remote_write",
        Capability.GOVERN_AGENTS,
        AgentAccessCommand,
        set_agents_paused,
        execution_notes=("Set the state rather than toggle it, so asking twice changes nothing.",),
        label="Pause or resume agents",
        variants=(("on", "Pause all agents"), ("off", "Resume agents")),
        state=agents_paused,
    ),
    CapabilitySpec(
        SET_AGENT_BLOCK,
        "Block one agent's client on MCP and the machine API, or let it back in.",
        "remote_write",
        Capability.GOVERN_AGENTS,
        AgentBlockCommand,
        set_agent_blocked,
        "key",
        target_label="Client",
        target_help="The client id of an agent HQ has seen.",
        execution_notes=(
            "Refuse a client HQ has never seen.",
            "Set the state rather than toggle it, so asking twice changes nothing.",
        ),
        label="Block or allow an agent",
        variants=(("on", "Block"), ("off", "Allow")),
        reached_from="agent_policy",
        state=blocked,
    ),
    CapabilitySpec(
        REMOVE_AGENT,
        "Remove one agent HQ has seen, with its devices and its own rules, so it is listed no more.",
        DESTRUCTIVE_EFFECT,
        Capability.GOVERN_AGENTS,
        AgentRemoveCommand,
        remove_agent,
        "key",
        target_label="Client",
        target_help="The client id of an agent HQ has seen.",
        execution_notes=(
            "Refuse a client HQ has never seen.",
            "Refuse a blocked agent, so removing it never lifts its block.",
            "Keep the audit log and what it asked for; a client that signs in again is a new agent.",
        ),
        label="Remove an agent",
        verb="Remove",
        reached_from="agent_policy",
    ),
    CapabilitySpec(
        APPROVE_AGENT_DEVICE,
        "Approve a tailnet device an agent called from, so its calls from there run, or withdraw the approval.",
        "remote_write",
        Capability.GOVERN_AGENTS,
        AgentDeviceApprovalCommand,
        set_device_approved,
        "key",
        target_label="Device",
        target_help="The record of a device an agent has called HQ from.",
        execution_notes=(
            "Refuse a device no agent has called from.",
            "Only the user an agent acts for approves the device it called from.",
            "Set the state rather than toggle it, so asking twice changes nothing.",
        ),
        label="Approve an agent's device",
        variants=(("on", "Approve device"), ("off", "Withdraw approval")),
        reached_from="agent_policy",
        state=device_approved,
    ),
    CapabilitySpec(
        BLOCK_AGENT_DEVICE,
        "Block one tailnet device an agent calls from, on MCP and the machine API, or let it back in.",
        "remote_write",
        Capability.GOVERN_AGENTS,
        AgentDeviceBlockCommand,
        set_device_blocked,
        "key",
        target_label="Device",
        target_help="The record of a device an agent has called HQ from.",
        execution_notes=(
            "Refuse a device no agent has called from.",
            "Set the state rather than toggle it, so asking twice changes nothing.",
        ),
        label="Block or allow an agent's device",
        variants=(("on", "Block device"), ("off", "Allow device")),
        reached_from="agent_policy",
        state=device_blocked,
    ),
    CapabilitySpec(
        SET_OWN_DEVICE,
        "Register a client as one of your own devices, so its token acts as you, or remove it.",
        "remote_write",
        Capability.GOVERN_AGENTS,
        OwnDeviceCommand,
        set_own_device,
        "key",
        target_label="Client",
        target_help="The client id Pocket ID registered for your device.",
        execution_notes=(
            "Register the device only as your own, never as another user's.",
            "Refuse a client id Pocket ID could not have registered.",
            "Set the state rather than toggle it, so asking twice changes nothing.",
        ),
        label="Register or remove your device",
        variants=(("on", "Register"), ("off", "Remove")),
        reached_from="agent_policy",
    ),
    CapabilitySpec(
        SET_POLICY,
        "Set whether one agent's call to one action runs, waits for approval, or is refused.",
        "remote_write",
        Capability.GOVERN_AGENTS,
        PolicyCommand,
        apply_changes,
        execution_notes=(
            "Change only the rules that differ from the current ones, each recorded on its own.",
            "Narrow only what Pocket ID granted: refuse a rule for an action the agent's grant does not carry.",
            "Refuse a rule for an action taken only in the browser, and approval for a read.",
        ),
        label="Set the agent policy",
    ),
)


# A host domain's own commands, kept here beside the rest of the host's: each
# domain's declaration names its function (``domains.HOST_DOMAINS``), so what
# reads a domain's commands finds these under it.


def dashboard() -> tuple[CapabilitySpec, ...]:
    return (
        # A person's own arrangement of HQ, kept by that person or an agent
        # they signed in (security.NEEDS_A_PERSON), and the dashboard's shared
        # settings: none of it reaches a spec or the controller.
        CapabilitySpec(
            pins.PIN,
            "Pin something so it sorts first for you, or unpin it.",
            "remote_write",
            Capability.PIN_FAVORITES,
            pins.PinCommand,
            pins.pin,
            execution_notes=("Pin it last among your pins of its type, or unpin it.",),
            label="Pin or unpin an item",
            verb="Pin",
            reached_from=ON_ITS_PAGE,
        ),
        CapabilitySpec(
            pins.MOVE,
            "Move one of your pins past its neighbor.",
            "remote_write",
            Capability.PIN_FAVORITES,
            pins.MoveCommand,
            pins.move_pin,
            execution_notes=("Swap it with the pin above or below, leaving the rest in place.",),
            label="Move a pin",
            reached_from=ON_ITS_PAGE,
        ),
        CapabilitySpec(
            pins.CHOOSE,
            "Choose exactly which things of one type you pin.",
            "remote_write",
            Capability.PIN_FAVORITES,
            pins.ChooseCommand,
            pins.choose,
            execution_notes=("Unpin what is not chosen and pin what is new, keeping the order of the rest.",),
            label="Choose pins",
            reached_from="dashboard",
        ),
        CapabilitySpec(
            action_items.SET_ASIDE,
            "Set action items aside for you until they get worse or change.",
            "remote_write",
            Capability.SET_ASIDE_ACTION_ITEMS,
            action_items.SetAsideCommand,
            action_items.set_items_aside,
            execution_notes=(
                "Set each named row aside as it was drawn.",
                "Set each named item aside as the queue holds it now.",
            ),
            label="Set aside action items",
            verb="Set aside",
            reached_from="action_items",
        ),
        CapabilitySpec(
            action_items.RESTORE,
            "Bring set-aside action items back into your queue.",
            "remote_write",
            Capability.SET_ASIDE_ACTION_ITEMS,
            action_items.RestoreCommand,
            action_items.restore,
            label="Bring back action items",
            verb="Bring back",
            reached_from="action_items",
        ),
        CapabilitySpec(
            DASHBOARD_SETTINGS,
            "Set the dashboard's weather location and its panel names.",
            "remote_write",
            Capability.SET_PREFERENCES,
            DashboardSettingsCommand,
            save_dashboard_settings,
            execution_notes=(
                "Refuse a weather location that is not a latitude and longitude in range.",
                "Name an unnamed panel by its default.",
            ),
            label="Save dashboard settings",
        ),
        CapabilitySpec(
            SET_THEME,
            "Choose whether your pages are light, dark, or follow the system.",
            "remote_write",
            Capability.SET_PREFERENCES,
            ThemeCommand,
            set_theme,
            execution_notes=("Set the theme rather than cycle it, so asking twice changes nothing.",),
            label="Choose a theme",
        ),
        CapabilitySpec(
            DASHBOARD_REFRESH,
            "Ask for a fresh reading of every dashboard panel a controller can read.",
            "infrastructure_change",
            Capability.MANAGE_INFRASTRUCTURE,
            DashboardRefreshCommand,
            request_dashboard_refresh,
            execution_notes=("Ask only for panels a controller has a way to read.",),
            label="Refresh the dashboard",
            verb="Refresh",
        ),
        CapabilitySpec(
            DASHBOARD_REFRESH_STALE,
            "Ask again for the dashboard panels whose reading is due.",
            "infrastructure_change",
            Capability.MANAGE_INFRASTRUCTURE,
            DashboardRefreshCommand,
            request_stale_panel_refresh,
            execution_notes=("Ask once for each panel that is due, readable and not already asked for.",),
            label="Refresh due readings",
        ),
    )


def watching() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            PROFILE_READ_NOW,
            "Ask HQ to read a user's GitHub profile now.",
            "infrastructure_change",
            # A public lookup that spends GitHub's allowance, and a wake of the
            # controller, as Refresh is.
            (Capability.LOOK_UP_PUBLIC_RECORDS, Capability.MANAGE_INFRASTRUCTURE),
            ProfileReadCommand,
            request_profile_read,
            execution_notes=(
                "Refuse a login no sign-in names.",
                "Ask for the profile reading now; the controller reads GitHub with its own credentials.",
            ),
            label="Refresh a GitHub profile",
            reached_from="watching",
            verb=CONTROLLER_REFRESH.words(),
        ),
    )


def tools() -> tuple[CapabilitySpec, ...]:
    return (
        # Capabilities that read something HQ does not hold. Both are `read`, so
        # neither takes an idempotency key and neither writes: asking a
        # registry the same question twice is the same question twice.
        CapabilitySpec(
            LOOK_UP_NAME,
            "Ask a public resolver what the internet returns for a hostname.",
            "read",
            Capability.LOOK_UP_PUBLIC_RECORDS,
            NameCommand,
            look_up_name,
            execution_notes=(
                "Validate the hostname before anything leaves this machine.",
                (
                    "Ask one resolver outside this network, so internal rewrites cannot "
                    "answer a question about the public internet."
                ),
                "Return the records as the resolver gave them, without the TTL, which this resolver fixes.",
            ),
            label="Look up a name",
        ),
        CapabilitySpec(
            LOOK_UP_ADDRESS,
            "Ask what name and which allocation a public address belongs to.",
            "read",
            Capability.LOOK_UP_PUBLIC_RECORDS,
            AddressCommand,
            look_up_address,
            execution_notes=(
                (
                    "Refuse a private address locally; nothing outside can describe it, "
                    "and asking would disclose it for no answer."
                ),
                "Read reverse DNS, which the address holder publishes and which usually carries a brand name.",
                (
                    "Read the RDAP allocation, which the registry publishes and which "
                    "carries the company. Either registry may fail without the other."
                ),
            ),
            label="Look up an address",
        ),
    )


def connections() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            VISIT_REFRESH,
            "Ask again for the readings one page is made of, where they are due.",
            "infrastructure_change",
            Capability.MANAGE_INFRASTRUCTURE,
            VisitRefreshCommand,
            request_visit_refresh,
            execution_notes=(
                "Find what the page is made of from what it is about; the request names no reading or connection.",
                "Ask only for what is due, read on a schedule, and read without logging in to a machine.",
                "Ask nothing while no controller will come to read it.",
            ),
            label="Read a page again",
            reached_from="control_plane:connections",
        ),
        CONTROLLER_REFRESH,
    )
