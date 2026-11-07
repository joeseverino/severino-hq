"""The shapes the connection, credential and tailnet resources answer in.

Their rows and records, and each part of them HQ builds, as
``resource_records`` declares the rest of the host's resources: the API
document types each read by them, and a test holds every answer on the
showcase data to its type.
"""

from typing import Annotated, Any, TypedDict

from pydantic import ConfigDict, with_config

from hq.domains.control_plane.provider_adapters.contracts import FAILURES, REFUSALS

from . import finding_model, findings
from .answer_fields import AsRead, ObservedAt
from .connection_catalog import AUTHORITY_LABELS, EVIDENCE_LABELS
from .connection_contracts import CREDENTIAL_MODELS, GRANT_MODELS
from .connection_lifecycle import Lifecycle
from .connection_reach import NETWORK_LABELS
from .credential_sight import STATE_LABELS
from .describe import Describe, listed
from .json_schemas import Written
from .resource_records import WrittenLink
from .topology_model import STANDINGS
from .ui import ended
from .workflows import WrittenAction


class TallyPart(TypedDict):
    count: Annotated[int, Describe("How many readings and records are in this state.")]
    state: Annotated[str, Describe(ended(f"The state, in words: {listed(STATE_LABELS.values())}"))]


class CredentialRow(TypedDict):
    provider: Annotated[
        str, Describe("The service the credential belongs to, such as `adguard`, which a read of one names it by.")
    ]
    label: Annotated[str, Describe("The service's name, as a page heads it.")]
    credential_refusal: Annotated[
        str, Describe("Why the service refused the credential itself; empty when it did not.")
    ]
    tally: Annotated[
        list[TallyPart], Describe("How many readings and records it reads in each state, for the states it has.")
    ]


class PartOfReading(TypedDict):
    """The part of a type a sweep could not read, and the permissions it needs."""

    name: Annotated[str, Describe("The part's name among its type's parts; empty for the whole type.")]
    label: Annotated[str, Describe("The part, in words.")]
    requires: Annotated[list[str], Describe("The permissions reading the part needs, as the service names them.")]
    provider: Annotated[
        str, Describe("The service whose credential reads the part, where it is not its type's own; else empty.")
    ]


class PartRefusalPart(TypedDict):
    kind: Annotated[str, Describe("The type the refused part belongs to.")]
    part: PartOfReading
    refusal: Annotated[str, Describe(f"How it was refused, {listed(FAILURES)}; empty when the cause is unknown.")]
    reason: Annotated[str, Describe("The service's words for the refusal; empty for none.")]
    scope: Annotated[
        str, Describe("What it was refused on, such as a zone, a hostname or a machine; empty for all of it.")
    ]
    connection_ref: Annotated[str, Describe("The connection it was refused to; empty for none.")]
    address: Annotated[str, Describe("The machine's address, when it was refused on a machine; else empty.")]


class SightPart(TypedDict):
    kind: Annotated[str, Describe("The reading or record type it could read.")]
    label: Annotated[str, Describe("The type it could read, in words.")]
    source: Annotated[
        str, Describe(ended(f"Whether it is a reading's type or a record's: {listed(('reading', 'resource'))}"))
    ]
    state: Annotated[str, Describe(ended(f"Whether the credential could read it: {listed(STATE_LABELS)}"))]
    state_label: Annotated[str, Describe("That, in the words its row shows.")]
    records: Annotated[int, Describe("How many records its last read stored.")]
    observed_at: ObservedAt | None
    error: Annotated[str, Describe("Why its last read failed, as the service said; empty when it did not.")]
    requires: Annotated[
        str, Describe("The permissions it needs, as the service names them in one line; empty for none.")
    ]
    refusal: Annotated[str, Describe(f"How the service refused the read, {listed(REFUSALS)}; empty when it did not.")]
    permissions: Annotated[list[str], Describe("The permissions `requires` names, one each.")]
    parts: Annotated[list[PartRefusalPart], Describe("Its parts that were refused while the rest read.")]
    remedy: Annotated[str, Describe("What would let HQ read all of it; empty when nothing is missing.")]


class CredentialSightPart(CredentialRow):
    """What a connection's credential can read, counted in each state."""

    manages: Annotated[list[str], Describe("The types of record its credential changes, in words.")]


class CredentialRecord(CredentialSightPart):
    """What one service's credential reads, each reading or listing it sees."""

    sights: Annotated[list[SightPart], Describe("Each reading or record type it could read.")]


class PolicyCounts(TypedDict):
    """How many grants, groups, tags and tests the policy holds."""

    grants: Annotated[int, Describe("How many grants the policy holds.")]
    groups: Annotated[int, Describe("How many groups the policy holds.")]
    tags: Annotated[int, Describe("How many tags the policy holds.")]
    tests: Annotated[int, Describe("How many tests the policy holds.")]


class PolicyNamePart(TypedDict):
    text: Annotated[str, Describe("A name as the policy writes it, such as a tag, a group or an address.")]
    link: Annotated[WrittenLink | None, Describe("The machine it stands for; null when it names none HQ knows.")]


class SettingPart(TypedDict):
    label: Annotated[str, Describe("The setting, in words.")]
    value: Annotated[str, Describe("Its value, as a page shows it.")]
    machines: Annotated[
        list[PolicyNamePart] | None, Describe("The machines its value names; null for a value that names none.")
    ]


# Each entry of the policy as written, beside what HQ makes of its sources and
# destinations; any other key is the policy's own.
@with_config(ConfigDict(extra="allow"))
class PolicyRulePart(TypedDict):
    src: Annotated[list[str], Describe("Its sources, as the policy writes them.")]
    dst: Annotated[list[str], Describe("Its destinations, as the policy writes them.")]
    sources: Annotated[list[PolicyNamePart], Describe("Each of its sources, with the machine it stands for.")]
    destinations: Annotated[list[PolicyNamePart], Describe("Each of its destinations, with the machine it stands for.")]


class GrantPortPart(TypedDict):
    entry: Annotated[str, Describe("A port entry, as the grant writes it.")]
    name: Annotated[str, Describe("What the port is for, such as `HTTPS`; empty when not known.")]


@with_config(ConfigDict(extra="allow"))
class PolicyGrantPart(PolicyRulePart):
    ports: Annotated[list[GrantPortPart], Describe("The ports it opens, each with what it is for.")]


class GroupPart(TypedDict):
    name: Annotated[str, Describe("The group's name, as the policy writes it, such as `group:admins`.")]
    members: Annotated[list[str], Describe("Its members, as the policy writes them.")]


class TagPart(TypedDict):
    name: Annotated[str, Describe("The tag's name, as the policy writes it, such as `tag:server`.")]
    owners: Annotated[list[str], Describe("Who may apply it, as the policy writes them.")]
    machines: Annotated[list[WrittenLink], Describe("The machines that carry it.")]


class UnreadPart(TypedDict):
    kind: Annotated[str, Describe("The tailnet reading's type.")]
    label: Annotated[str, Describe("The tailnet reading, in words.")]
    state: Annotated[str, Describe(ended(f"Why it was not read: {listed(STATE_LABELS)}"))]
    state_label: Annotated[str, Describe("Why, in the words its row shows.")]
    refusal: Annotated[str | None, Describe(f"How the service refused it, {listed(REFUSALS)}; null when it did not.")]
    error: Annotated[str | None, Describe("The service's words for the failure; null for none.")]
    remedy: Annotated[str | None, Describe("What would let HQ read it; null when nothing would.")]
    requires: Annotated[str | None, Describe("The permissions reading it needs; null for none.")]


class Tailnet(TypedDict):
    """The tailnet's policy and what HQ makes of it."""

    known: Annotated[bool, Describe("Whether the policy has been read: true once it holds a group, tag or grant.")]
    declaration: Annotated[
        str | None, Describe("The key of HQ's record of the policy, which a change to it edits; null when none.")
    ]
    counts: PolicyCounts
    settings: Annotated[list[SettingPart], Describe("The tailnet's settings.")]
    grants: Annotated[list[PolicyGrantPart], Describe("The policy's grants as written.")]
    ssh_rules: Annotated[list[PolicyRulePart], Describe("The policy's SSH rules as written.")]
    groups: Annotated[list[GroupPart], Describe("The policy's groups.")]
    tags: Annotated[list[TagPart], Describe("The policy's tags.")]
    services: Annotated[list[AsRead], Describe("The tailnet's services, as read.")]
    app_connectors: Annotated[list[AsRead], Describe("The tailnet's app connectors, as read.")]
    funnel: Annotated[
        list[str] | None,
        Describe(
            "Each node attribute target holding funnel, which a node needs to publish to the internet; "
            "empty when none does, null when the reading does not say."
        ),
    ]
    tests: Annotated[list[AsRead], Describe("The policy's tests, as written.")]
    locked_out: Annotated[list[str], Describe("The machines tailnet lock keeps off the tailnet.")]
    findings: Annotated[
        list[findings.SerializedFinding], Describe("The open problems about the tailnet that the reader may see.")
    ]
    unread: Annotated[list[UnreadPart], Describe("Each tailnet reading that could not be read.")]
    credential_refusal: Annotated[
        str | None, Describe("Why the service refused the tailnet's credential; null when it did not.")
    ]


class ConnectionStandingRow(TypedDict):
    id: Annotated[str, Describe("The connection's id across HQ, which a read of one also names it by.")]
    connection_ref: Annotated[
        str | None,
        Describe(
            "The ref its controller and HQ's audit log name it by, which a read of one names it by; null for none."
        ),
    ]
    label: Annotated[str, Describe("The connection's name, as its row heads it.")]
    kind: Annotated[str, Describe("What it connects to, such as the service a controller's connection reads.")]
    family: Annotated[
        str,
        Describe("The connection type's name, such as `infrastructure.controllers`; `family_label` says it in words."),
    ]
    status: Annotated[str, Describe(ended(f"How the connection stands: {listed(STANDINGS)}"))]
    status_label: Annotated[str, Describe("How the connection stands, in the words its row shows.")]
    detail: Annotated[
        str, Describe("A line on what it last reported, such as how much it holds; empty when there is nothing to add.")
    ]
    observed_at: ObservedAt | None
    open_problems: Annotated[int, Describe("How many open problems about the connection the reader may see.")]


class FactPart(TypedDict):
    label: Annotated[str, Describe("What the fact is about, in a few words.")]
    value: Annotated[str, Describe("The fact, as the connection reported it.")]


class ConnectionLinkPart(TypedDict):
    label: Annotated[str, Describe("What it is, by name.")]
    url: Annotated[str, Describe("Its page; empty for none.")]
    resource_key: Annotated[str, Describe("The key of HQ's record it is; empty when it is none.")]


class AbilityPart(TypedDict):
    name: Annotated[str, Describe("What it lets HQ do, by its stable name.")]
    label: Annotated[str, Describe("What it lets HQ do, in words.")]
    effect: Annotated[str, Describe("What using it does, such as `read` or `remote_write`.")]
    required_scopes: Annotated[list[str], Describe("The permissions it needs, as the service names them.")]
    grant: Annotated[str | None, Describe(f"How the service grants it: {listed(GRANT_MODELS)}; null when not said.")]
    evidence: Annotated[str, Describe(ended(f"How HQ knows whether it holds: {listed(EVIDENCE_LABELS)}"))]
    available: Annotated[
        bool | None, Describe("Whether the credential holds every permission it needs; null when not known.")
    ]
    missing_scopes: Annotated[list[str], Describe("The permissions it needs that the credential lacks.")]
    capability: Annotated[str | None, Describe("The command it lets HQ run; null for none.")]
    subject_resource: Annotated[str | None, Describe("The list of records it governs; null for none.")]
    action: WrittenAction | None


class SeenPart(SightPart):
    freshness: Annotated[str, Describe("How current its last read is, in one word.")]
    freshness_label: Annotated[str, Describe("How current its last read is, in the words its row shows.")]


class ReadPart(TypedDict):
    label: Annotated[str, Describe("What it reads, in words.")]
    sight: Annotated[SeenPart | None, Describe("What was last read of it; null when nothing reads it yet.")]
    ability: Annotated[AbilityPart | None, Describe("The permissions behind it; null when none are named.")]


class ReachMachinePart(TypedDict):
    name: Annotated[str, Describe("The machine's name.")]
    url: Annotated[str, Describe("The machine's page in HQ; empty for none.")]


class ReachPart(TypedDict):
    network: Annotated[
        str | None, Describe(f"The network it is reached over: {listed(NETWORK_LABELS)}; null when not known.")
    ]
    label: Annotated[str | None, Describe("That network, in words; null when not known.")]
    summary: Annotated[str | None, Describe("How it is reached, in one line; null when not known.")]
    host: Annotated[str, Describe("The host its address names.")]
    address: Annotated[str | None, Describe("The address that host answers at; null when not read.")]
    machine: Annotated[ReachMachinePart | None, Describe("The machine it is reached at; null when not known.")]
    peer_path: Annotated[
        str | None, Describe("`direct`, `relayed` or `negotiating` over the tailnet; null off the tailnet.")
    ]
    direct_endpoint: Annotated[str | None, Describe("The endpoint a direct tailnet path uses; null for none.")]
    relay: Annotated[str | None, Describe("The relay a tailnet path goes through; null for none.")]
    handshake: Annotated[str | None, Describe("When the tailnet peers last shook hands; null when not read.")]
    rx_bytes: Annotated[int | None, Describe("The bytes received from it over the tailnet; null when not read.")]
    tx_bytes: Annotated[int | None, Describe("The bytes sent to it over the tailnet; null when not read.")]


# Closed as `Findings` is, so the operator steps it holds are written as the
# same component there and here.
@with_config(ConfigDict(extra="forbid"))
class CredentialFixPart(TypedDict):
    missing: Annotated[list[str], Describe("The permissions the service refused while the credential is valid.")]
    unseen: Annotated[list[str], Describe("What those permissions would let HQ see.")]
    refused: Annotated[
        str | None, Describe("The service's words when it refused the credential; null when it did not.")
    ]
    expires_at: Annotated[str | None, Describe("When the credential runs out, an ISO 8601 date-time; null for never.")]
    expiring: Annotated[bool, Describe("Whether it runs out soon enough to replace now.")]
    expired: Annotated[bool, Describe("Whether it has run out.")]
    permissions: Annotated[list[str], Describe("Every permission the replacement is issued with.")]
    steps: Annotated[
        list[Annotated[dict[str, Any], Written(finding_model.OperatorStep)]],
        Describe("What a person does to replace it, in order."),
    ]


class LastActivityPart(TypedDict):
    summary: Annotated[str, Describe("What the entry records, in a line.")]
    created_at: Annotated[str, Describe("When it was recorded, an ISO 8601 date-time.")]
    audit_id: Annotated[int, Describe("The entry's id in HQ's audit log.")]


class ConnectionStandingRecord(ConnectionStandingRow):
    """One connection whole: what its credential sees and changes, how it is reached, and the fix."""

    controller_id: Annotated[str | None, Describe("The controller that reported it; null for one read without one.")]
    lifecycle: Annotated[str, Describe(ended(f"Where it is in its life: {listed(Lifecycle)}"))]
    endpoint: Annotated[str | None, Describe("The address HQ reaches the service at; null when none is shown.")]
    authority: Annotated[
        str, Describe(ended(f"What HQ knows of the credential's permissions: {listed(AUTHORITY_LABELS)}"))
    ]
    credential_model: Annotated[
        str | None,
        Describe(
            f"What sort of credential it is, {listed(CREDENTIAL_MODELS)}, the last for one the service refused; "
            "null when that is not known."
        ),
    ]
    scopes_known: Annotated[
        bool,
        Describe("Whether the service reported the credential's permissions, so an empty `granted_scopes` means none."),
    ]
    granted_scopes: Annotated[list[str], Describe("The permissions the credential carries, as the service names them.")]
    facts: Annotated[list[FactPart], Describe("What the connection reported of itself.")]
    actions: Annotated[
        list[WrittenAction],
        Describe("What the reader may do to the connection, each already checked against the reader's permissions."),
    ]
    dependencies: Annotated[list[ConnectionLinkPart], Describe("What the connection depends on.")]
    targets: Annotated[list[ConnectionLinkPart], Describe("What the connection points at.")]
    reads: Annotated[list[ReadPart], Describe("What its credential reads.")]
    changes: Annotated[list[AbilityPart], Describe("What its credential may change.")]
    family_label: Annotated[str, Describe("The connection type's name, in words.")]
    used_for: Annotated[str | None, Describe("The part of HQ that uses it, as the navigation names it; null for none.")]
    secret_store: Annotated[str | None, Describe("Where its credential is kept; null when its type names no place.")]
    reach: Annotated[ReachPart | None, Describe("How HQ reaches its address; null when that was not worked out.")]
    sight: Annotated[
        CredentialSightPart | None, Describe("What its credential can read; null when nothing reads through it.")
    ]
    would_also_see: Annotated[
        list[str], Describe("What the credential would also read with more permissions, each naming them.")
    ]
    credential_fix: Annotated[
        CredentialFixPart | None, Describe("How to fix its credential; null when it needs no fix.")
    ]
    last_activity: Annotated[LastActivityPart | None, Describe("The latest audit entry about it; null for none.")]
    read_now: WrittenAction | None
    read_requested_at: Annotated[
        str | None,
        Describe(
            "When a refresh of it was asked for that has not run yet, an ISO 8601 date-time; null when none waits."
        ),
    ]


__all__ = [
    "ConnectionStandingRecord",
    "ConnectionStandingRow",
    "CredentialRecord",
    "CredentialRow",
    "Tailnet",
]
