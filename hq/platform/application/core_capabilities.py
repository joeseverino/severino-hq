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
from .agent_access import SET_AGENT_ACCESS, AgentAccessCommand, agents_paused, set_agents_paused
from .appearance import SET_THEME, ThemeCommand, set_theme
from .approvals import APPROVE, DESTRUCTIVE_EFFECT, REJECT, ApproveCommand, RejectCommand, approve, reject
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
from .lookup import (
    LOOK_UP_ADDRESS,
    AddressCommand,
    NameCommand,
    look_up_address,
    look_up_name,
)
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
from .sync import HQSyncCommand, execute_hq_sync
from .tailnet import POLICY_KIND as TAILNET_POLICY_KIND, TAILNET_KIND
from .visit_refresh import VISIT_REFRESH, VisitRefreshCommand, request_visit_refresh
from .zones import ADOPT_ZONE, AdoptZoneCommand, adopt_zone

# Named, so prose that tells a person which button to press reads its words.
CONTROLLER_REFRESH = CapabilitySpec(
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
    label="Refresh",
    variants=(("all", "Refresh all"),),
    wants="refresh what HQ reads",
)

_RECEIPT_FILE_NOTES = (
    "Refuse base64 text longer than a file within the limit encodes to, before decoding it.",
    "Refuse a file over the limit, or of a type a receipt cannot be.",
    "Record the file's name, size and SHA-256 digest; its bytes are kept only as the receipt's file.",
)

CORE_CAPABILITY_SPECS = (
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
            "Ask nothing while no controller is arriving to read it.",
        ),
        label="Read a page again",
        reached_from="control_plane:connections",
    ),
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
        "Whether HQ's dependency update run opens pull requests in a project's repository, from its next run.",
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
        label="Dependency updates",
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
        verb="Edit",
        wants="change the expected settings of {target}",
    ),
    CapabilitySpec(
        ADOPT,
        "Manage a record HQ found live, starting from exactly what is live.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        AdoptCommand,
        adopt,
        subject_resource="infrastructure.resources",
        execution_notes=(
            "Read the record again as HQ last found it live, never from the request.",
            "Write HQ's copy equal to it, so applying it changes nothing.",
        ),
        label="Manage in HQ",
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
        subject_resource="infrastructure.resources",
        execution_notes=(
            "Find the hostname's records that HQ does not manage yet.",
            "Adopt each one through a connection that manages, in one transaction.",
        ),
        label="Manage a service in HQ",
    ),
    CapabilitySpec(
        ADOPT_ZONE,
        "Manage a domain HQ found live, and every record in it, exactly as they are.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        AdoptZoneCommand,
        adopt_zone,
        subject_resource="infrastructure.resources",
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
        "infrastructure.resources",
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
        "infrastructure.resources",
        target_label="Record",
        target_help="The record to apply.",
        label=OperationRequest.Action.RECONCILE.label,
        wants="apply HQ's settings to {target}",
    ),
    CONTROLLER_REFRESH,
    # A person's own arrangement of HQ, held only by that person in the browser
    # (security.HELD_ONLY_AT): none of it reaches a spec or the controller.
    CapabilitySpec(
        pins.PIN,
        "Pin something so it sorts first for you, or unpin it.",
        "remote_write",
        Capability.PIN_FAVORITES,
        pins.PinCommand,
        pins.pin,
        execution_notes=("Pin it last among your pins of its type, or unpin it.",),
        label="Pin",
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
        label="Set aside",
        reached_from="action_items",
    ),
    CapabilitySpec(
        action_items.RESTORE,
        "Bring set-aside action items back into your queue.",
        "remote_write",
        Capability.SET_ASIDE_ACTION_ITEMS,
        action_items.RestoreCommand,
        action_items.restore,
        label="Bring back",
        reached_from="action_items",
    ),
    CapabilitySpec(
        PROFILE_READ_NOW,
        "Ask the controller to read a signed-in person's GitHub profile now.",
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
        label="Read a GitHub profile now",
        reached_from="watching",
        verb=CONTROLLER_REFRESH.words(),
    ),
    CapabilitySpec(
        ACTION_CAPABILITY[OperationRequest.Action.DELETE],
        "Remove the live thing this record describes, then remove the record from HQ.",
        DESTRUCTIVE_EFFECT,
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_removal,
        "key",
        "infrastructure.resources",
        target_label="Record",
        target_help="The record to remove.",
        label="Remove a record",
        verb=OperationRequest.Action.DELETE.label,
        # A record HQ only watches is forgotten, and nothing live is touched.
        variants=(("forget", "Stop tracking in HQ"),),
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
            "infrastructure.resources",
            target_label="Container",
            target_help="The container to act on.",
            target_query=(("kind", CONTAINER_KIND),),
            execution_notes=(
                "Refuse a container switched off in HQ, or one Portainer cannot do this to.",
                "Queue it once for the controller, which does it outside this request.",
            ),
            label=action.label,
        )
        for action, handler, summary, effect in (
            (
                OperationRequest.Action.RESTART,
                request_restart,
                "Restart the live thing a record names.",
                "infrastructure_change",
            ),
            (
                OperationRequest.Action.START,
                request_start,
                "Start the live thing a record names.",
                "infrastructure_change",
            ),
            # A stop takes the thing away from whoever uses it until someone
            # starts it again: an agent's waits for a person by default, and a
            # person's is confirmed on a page of its own.
            (OperationRequest.Action.STOP, request_stop, "Stop the live thing a record names.", DESTRUCTIVE_EFFECT),
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
        verb="Allow in the tailnet policy",
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
        "infrastructure.resources",
        target_label="Tailscale policy",
        target_help="The Tailscale policy to change.",
        target_query=(("kind", TAILNET_POLICY_KIND),),
        execution_notes=(
            "Read HQ's copy of the policy and find the groups with no members that a rule names.",
            "Refuse when such a group is named anywhere else, since removing it would change what the policy means.",
            (
                "Strike them from their rules, dropping a rule left admitting nobody, and "
                "propose the changed policy, which a person approves."
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
        "infrastructure.resources",
        target_label="Certificate",
        target_help="The certificate to renew.",
        target_query=(("kind", CERTIFICATE_KIND),),
        execution_notes=(
            "Read the certificate's settings and check its renewal rules.",
            "Queue one renewal for the controller, which does the work outside this request.",
            "Return what was queued and why, recorded under whoever asked.",
        ),
        label=OperationRequest.Action.RENEW.label,
        wants="renew the certificate {target}",
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
        "infrastructure.resources",
        target_label="Certificate",
        target_help="The uploaded certificate record to hold it for.",
        target_query=(("kind", UPLOADED_CERTIFICATE_KIND),),
        execution_notes=(
            "Refuse a certificate that has expired or a key that does not match it.",
            "Seal both with the secret store key, refusing when none is configured.",
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
        label="Upload a receipt",
    ),
    CapabilitySpec(
        RECEIPT_REPLACE,
        "Replace a receipt's file, and change its details with it.",
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
    # A person's own arrangement of their pages, held only in the browser
    # (security.HELD_ONLY_AT).
    CapabilitySpec(
        DASHBOARD_MACHINE,
        "Show a machine's telemetry on the dashboard, or take it off.",
        "remote_write",
        Capability.SET_PREFERENCES,
        DashboardMachineCommand,
        select_dashboard_machine,
        "key",
        "infrastructure.resources",
        target_label="Machine",
        target_help="The machine whose telemetry to show.",
        target_query=(("kind", MACHINE_KIND),),
        execution_notes=("Refuse a record that is not an enabled machine.",),
        label="Show on the dashboard",
        variants=(("on", "Show on the dashboard"), ("off", "Take off the dashboard")),
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
        approve,
        execution_notes=(
            "Refuse a request that was decided, expired, or asked for by whoever is deciding.",
            "Refuse when what it changes moved on after it was asked for.",
            "Run the held call once, recorded under whoever asked and whoever agreed.",
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
        reject,
        execution_notes=("Record the refusal, who made it and the note, and apply nothing.",),
        label="Reject a held change",
        reached_from="action_items",
    ),
    CapabilitySpec(
        DASHBOARD_SETTINGS,
        "Set where the dashboard reads the weather for, and what its panels are called.",
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
        "Draw the signed-in person's pages in the system's theme, light or dark.",
        "remote_write",
        Capability.SET_PREFERENCES,
        ThemeCommand,
        set_theme,
        execution_notes=("Set the theme rather than cycle it, so asking twice changes nothing.",),
        label="Choose a theme",
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
        variants=(("on", "Block all agents"), ("off", "Allow agents")),
        state=agents_paused,
    ),
    CapabilitySpec(
        SET_POLICY,
        "Set whether an agent's call runs, waits for a person, or is refused, by surface or by agent.",
        "remote_write",
        Capability.GOVERN_AGENTS,
        PolicyCommand,
        apply_changes,
        execution_notes=(
            "Change only the rules that differ from what holds, each recorded on its own.",
            "Refuse a rule no agent could hold, approval on a read, and a rule for an agent its grant does not allow.",
        ),
        label="Set agent policy",
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
            (
                "Ask one resolver outside this network, so internal rewrites cannot "
                "answer a question about the public internet."
            ),
            (
                "Return the records as the resolver gave them, with no TTL: this "
                "resolver reports a constant, which is not a measurement."
            ),
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
