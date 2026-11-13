"""The shapes the host's own resources answer in: each list's row and each record.

A row is what a list read keeps of each item unless the caller names its own
fields, so its keys are the kind's ``list_fields``; a record is what a read of
one answers, and extends the row where it is the item itself. The API document
types each read by them, and a test holds every answer on the showcase data to
its type. Each part of a record HQ builds is a type here too, so every object
the API document writes names its fields; a part in its source's own shape is
``AsRead``.

Each field says what it holds where it is declared, and a closed set of values
is named from the constant that holds it (``describe.listed``).
"""

from typing import Annotated, Any, NotRequired, TypedDict

from hq.domains.control_plane.models import OperationRequest
from hq.domains.control_plane.observations.contract import READERS, READING_FACETS
from hq.domains.control_plane.provider_adapters.contracts import REFUSALS

from .answer_fields import AsRead, ObservedAt, Since, UpdatedAt
from .credential_sight import STATE_LABELS
from .describe import Describe, listed
from .documentation import (
    DocEnvironment,
    DocId,
    DocSensitivity,
    DocStatus,
    DocType,
    DocumentationTitle,
    ExternalUrl,
    GithubPath,
    LastReviewed,
    ObsidianPath,
)
from .entity_links import EntityLink
from .json_schemas import Written
from .plugins import ATTENTION_ORDER
from .record_fields import Notes, PublishedAt
from .service_health import ATTENTION, GOOD, SERIOUS, UNKNOWN
from .services import HQ_MARK, OBSERVED_MARK
from .topology_model import STANDINGS
from .ui import ended
from .upgrades import HIGH, LOW, MAJOR, MEDIUM, MINOR, PATCH, UNKNOWN_CHANGE
from .workflows import WrittenAction

# What a container row and a machine's list of its containers say alike.
type ComposeProject = Annotated[str, Describe("The compose project it belongs to; empty for one started on its own.")]

# A name and the page it opens, as ``EntityLink`` says it.
WrittenLink = Annotated[dict[str, Any], Written(EntityLink)]

# ----- Records HQ manages -----------------------------------------------------


class ConditionPart(TypedDict):
    type: Annotated[
        str,
        Describe(ended(f"What the condition says of the record, such as {listed(('Ready', 'Drifted', 'Degraded'))}")),
    ]
    status: Annotated[bool, Describe("Whether the condition holds now.")]
    reason: Annotated[str, Describe("Why, in the controller's one word for it; empty for none.")]
    message: Annotated[str, Describe("The controller's sentence on it; empty for none.")]
    since: NotRequired[
        Annotated[str, Describe("When it began to hold as it does, an ISO 8601 date-time, once HQ has stored it.")]
    ]


# What an operation's item and its row of a record's history say alike.
type OperationReason = Annotated[str, Describe("Why it is done, in the asker's words; empty for none.")]
type OperationId = Annotated[str, Describe("The operation's id.")]
type OperationAction = Annotated[str, Describe(ended(f"What it does: {listed(OperationRequest.Action.values)}"))]
type OperationState = Annotated[str, Describe(ended(f"Where it stands: {listed(OperationRequest.State.values)}"))]
type RequestedActor = Annotated[str, Describe("Who asked for it, by the name HQ knows them by.")]
type RequestedInterface = Annotated[str, Describe("Which door it was asked through, such as `api` or `web`.")]
type AskedAt = Annotated[str, Describe("When it was asked for, an ISO 8601 date-time.")]
type CompletedAt = Annotated[str | None, Describe("When it finished, an ISO 8601 date-time; null until it has.")]
type AttemptCount = Annotated[int, Describe("How many times a controller has tried it.")]


class OperationSummary(TypedDict):
    """One operation in a record's history: how it ended, in a sentence, and what it found."""

    id: OperationId
    action: OperationAction
    action_label: Annotated[str, Describe("What it does, in words.")]
    state: OperationState
    state_label: Annotated[str, Describe("How it ended or where it stands, in the words its pill shows.")]
    tone: Annotated[
        str, Describe(ended(f"How its pill is colored: {listed(('paused', 'active', 'serious', 'attention', 'good'))}"))
    ]
    headline: Annotated[str, Describe("How it ended, in a sentence.")]
    automatic: Annotated[bool, Describe("Whether the controller ran it on its own, unasked.")]
    requested_actor: RequestedActor
    requested_interface: RequestedInterface
    by: Annotated[str, Describe("Who asked for it, as HQ shows it: `Automatic` for the controller.")]
    approved_by: Annotated[str, Describe("Who agreed to a change a credential asked for; empty for anything else.")]
    created_at: AskedAt
    completed_at: CompletedAt
    attempt_count: AttemptCount
    reason: OperationReason
    condition: Annotated[ConditionPart | None, Describe("The first condition its run reported holding; null for none.")]
    affected: Annotated[list[AsRead], Describe("Those of `evidence` that do not match what HQ expects of them.")]
    evidence: Annotated[list[AsRead], Describe("What uses the record, each as the controller found it on its run.")]
    matched: Annotated[int, Describe("How many of `evidence` match what HQ expects of them.")]
    expected_fingerprint_sha256: Annotated[
        str, Describe("The SHA-256 fingerprint of the certificate the run expected to find; empty for none.")
    ]
    raw_result: Annotated[
        dict[str, Any], Describe("What the controller reported of its run, leaving out a certificate's file.")
    ]


class HealthPart(TypedDict):
    """Its health as last read, and the condition behind it."""

    state: Annotated[str, Describe("Its health in one word, such as `healthy`, `pending` or `unknown`.")]
    label: Annotated[str, Describe("Its health in the words a page shows, such as `Working`.")]
    reason: Annotated[str, Describe("The reason on the condition behind it, as reported; empty for none.")]
    message: Annotated[str, Describe("The message on that condition, or why it has not been read yet; empty for none.")]


class ManagedResourceRow(TypedDict):
    key: Annotated[str, Describe("The key HQ files the record under, which a read of one names it by.")]
    kind: Annotated[str, Describe("The record's type, such as `adguard.rewrite`.")]
    enabled: Annotated[bool, Describe("Whether HQ applies this record; false, HQ keeps it without applying it.")]
    in_sync: Annotated[bool, Describe("Whether the latest change to HQ's copy has been applied and confirmed back.")]
    health: HealthPart
    public_effect: Annotated[
        bool,
        Describe("Whether applying a record of this type changes what the internet sees, such as a public DNS record."),
    ]
    last_observed_at: ObservedAt | None


class ManagedResourceItem(ManagedResourceRow):
    """One managed record whole: what HQ expects of it and what was last read."""

    id: Annotated[str, Describe("The record's id in HQ's database.")]
    spec: Annotated[
        dict[str, Any],
        Describe("What HQ expects of it, in the shape its type takes; empty for a type whose settings HQ withholds."),
    ]
    status: Annotated[
        dict[str, Any],
        Describe(
            "What was last read of it, as its type reports it. A certificate's file is left out, "
            "`certificate_available` saying HQ holds one."
        ),
    ]
    conditions: Annotated[list[ConditionPart], Describe("Its conditions as the controller last reported them.")]
    generation: Annotated[int, Describe("Which version of HQ's copy this is, counting up from 1 as it changes.")]
    observed_generation: Annotated[
        int,
        Describe(
            "Which version of HQ's copy was last applied and confirmed back. It equals the current "
            "version when no change waits to apply."
        ),
    ]
    updated_at: UpdatedAt


class ManagedResourceRecord(TypedDict):
    """One managed record, what HQ derives from it, and the operations run on it."""

    resource: ManagedResourceItem
    derived: Annotated[
        dict[str, Any],
        Describe(
            "What HQ derives from the record. It holds health, allowed actions, machines and services, "
            "expected beside live, certificate expiry, pending approvals and what uses it."
        ),
    ]
    operations: Annotated[list[OperationSummary], Describe("The latest operations that changed it, newest first.")]


# ----- Services and paths -----------------------------------------------------


class OriginPart(TypedDict):
    """Where a request for the name is finally served, and what serves it there."""

    address: Annotated[
        str,
        Describe("Where a request for the name is finally served: the address a proxy forwards to or a record names."),
    ]
    host: Annotated[str, Describe("The machine at that address; empty when it matches none.")]
    container: Annotated[str, Describe("The container serving it there; empty when none is known.")]


class ServiceRow(TypedDict):
    hostname: Annotated[str, Describe("The service's hostname, which a read of one names it by.")]
    status: Annotated[
        str,
        Describe(
            f"How the service stands by its health rule: {listed((GOOD, ATTENTION, SERIOUS, UNKNOWN))}, "
            "the last when nothing reads its health."
        ),
    ]
    status_label: Annotated[str, Describe("The service's health in the words its row shows, such as `Working`.")]
    origin: OriginPart | None
    project: Annotated[str | None, Describe("The name of the project published at this name; null for none.")]
    faults: Annotated[list[str], Describe("What is not set up right, each a sentence; empty when nothing is.")]


class FacetPart(TypedDict):
    id: Annotated[str, Describe("Which part of the service it is, such as `dns`, `proxy`, `certificate` or `runtime`.")]
    label: Annotated[str, Describe("That part in the words its card is headed with, such as `DNS`.")]
    present: Annotated[bool, Describe("Whether any record of HQ's supplies it.")]
    state: Annotated[
        str,
        Describe(
            f"How the records behind it stand: {listed((GOOD, ATTENTION, SERIOUS))}; empty when none supplies it."
        ),
    ]
    resources: Annotated[list[str], Describe("The keys of HQ's records that supply it.")]


class ServiceItem(ServiceRow):
    """One service name whole, each facet with the records behind it."""

    mark: Annotated[
        str,
        Describe(
            f"`{HQ_MARK}` for HQ's own name, `{OBSERVED_MARK}` for a name only a reading holds, "
            "empty for any other service."
        ),
    ]
    facets: Annotated[
        list[FacetPart],
        Describe("Each part the service is made of, such as its DNS record, proxy, certificate and container."),
    ]


class HopSourcePart(TypedDict):
    """Where HQ read a hop: a reading, or HQ's own record."""

    kind: Annotated[str, Describe("The type of the reading or record it was read from.")]
    label: Annotated[str, Describe("That type, in words.")]
    connection: Annotated[str, Describe("The connection the reading came through; empty for HQ's own record.")]
    observed_at: ObservedAt | None
    declared: Annotated[
        str, Describe("The key of HQ's record it comes from, when HQ holds it rather than reads it; else empty.")
    ]


class CertificatePart(TypedDict):
    role: Annotated[str, Describe("What the certificate is at the hop, such as the edge's or the origin's.")]
    name: Annotated[str, Describe("The name the certificate is for; empty when not read.")]
    issuer: Annotated[
        str, Describe("Who issued it, or 'uploaded' for one uploaded to the proxy that serves it; empty when not read.")
    ]
    expiry: Annotated[str, Describe("When it runs out, as read; empty when not read.")]
    source: Annotated[HopSourcePart | None, Describe("Where HQ read the certificate; null when nothing says.")]
    unread: Annotated[str, Describe("Why HQ cannot say what certificate the hop presents; empty when it can.")]


class OverlayPart(TypedDict):
    relation: Annotated[str, Describe("What it is to the hop, such as an access gate.")]
    link: WrittenLink | None


class HopCheckPart(TypedDict):
    state: Annotated[str, Describe("Whether the request agrees with the readings at the hop, in one word.")]
    label: Annotated[str, Describe("That, in the words the hop shows.")]
    detail: Annotated[str, Describe("What the request showed beside what was read.")]
    step: Annotated[str, Describe("What you do to settle a disagreement; empty for none.")]


class EvidencePart(TypedDict):
    label: Annotated[str, Describe("What the request carried, such as a header or an address.")]
    value: Annotated[str, Describe("Its value, as the request carried it.")]
    detail: Annotated[str, Describe("A line on what HQ makes of it; empty for none.")]
    role: Annotated[
        str,
        Describe("For an address the request was forwarded through: `judged`, `proxy` or `ignored`; else empty."),
    ]


class LayerPart(TypedDict):
    id: Annotated[str, Describe("The layer's stable id, such as `edge` or `identity`.")]
    label: Annotated[str, Describe("The layer, in words.")]
    state: Annotated[
        str,
        Describe("`holds`, `does-not`, or `unknown` when what decides it did not answer."),
    ]
    detail: Annotated[str, Describe("Why it was decided so, in a sentence.")]
    evidence: Annotated[str, Describe("The value it was decided from; empty for none.")]
    boundary: Annotated[str, Describe("The security boundary it belongs to; empty for none.")]
    mechanism: Annotated[str, Describe("What enforces it; empty for none.")]
    rules: Annotated[list[str], Describe("The rules behind it, where what decides it has rules.")]


class ObservedPart(TypedDict):
    relation: Annotated[str, Describe("What the reading says the hostname is to it, such as `Served by`.")]
    name: Annotated[str, Describe("What answers for the hostname, by name.")]
    link: WrittenLink | None
    detail: Annotated[str, Describe("A line on what it does for the hostname; empty for none.")]
    source: Annotated[HopSourcePart | None, Describe("Where HQ read it; null when nothing says.")]


class RoutePart(TypedDict):
    via: Annotated[str, Describe("What the route starts from, such as one DNS answer.")]
    port: Annotated[int | None, Describe("The port a TCP or UDP forward listens on; null for any other route.")]
    line: Annotated[str, Describe("The route in one line, hop to hop.")]
    hops: Annotated[list[HopRow], Describe("The route's hops, in the order a request takes them.")]


class PathRecord(TypedDict):
    """The path a request for a hostname takes, hop by hop on each route."""

    hostname: Annotated[str, Describe("The hostname the path is walked for.")]
    line: Annotated[str, Describe("The first route in one line, hop to hop; empty when no route was walked.")]
    redirects_to: Annotated[str, Describe("The hostname a route redirects the request to; empty when none does.")]
    machine: Annotated[str, Describe("The machine the first route to reach one ends on; empty when none does.")]
    unread: Annotated[
        list[str], Describe("What HQ could not read or walk, each a sentence; empty when the path is whole.")
    ]
    observed: Annotated[
        list[ObservedPart], Describe("What the readings that name the hostname as something that answers hold.")
    ]
    routes: Annotated[
        list[RoutePart],
        Describe("Each route a request can take, from one DNS answer to what serves the name."),
    ]


class ServiceRecord(TypedDict):
    """One service name and the path a request for it takes."""

    service: ServiceItem
    path: PathRecord


# Where a hop is one the calling request walked, which a path of a service has none of.
_WALKED = "Present only on a hop the calling request walked."


class HopRow(TypedDict):
    """One hop of HQ's own path; a hop walked for the calling request adds what it showed there."""

    step: Annotated[str, Describe("What the hop is, such as `dns`, `ingress`, `machine` or `container`.")]
    label: Annotated[str, Describe("The hop in the words its step is headed with.")]
    name: Annotated[
        str, Describe("Which one it is, such as a hostname or a machine's name; empty when `label` says it.")
    ]
    link: Annotated[WrittenLink | None, Describe("The page of what the hop names; null when it has none.")]
    detail: Annotated[
        str, Describe("A line on what it does there, such as where it forwards; empty when there is nothing to add.")
    ]
    source: Annotated[HopSourcePart | None, Describe("Where HQ read the hop; null when nothing says.")]
    certificate: Annotated[
        CertificatePart | None,
        Describe("The certificate the hop presents, or why HQ cannot say; null when it presents none."),
    ]
    unread: Annotated[str, Describe("What HQ could not read about the hop, a sentence; empty when nothing is missing.")]
    overlays: Annotated[list[OverlayPart], Describe("What sits over the hop, such as an access gate.")]
    check: NotRequired[
        Annotated[
            HopCheckPart,
            Describe(f"Whether what the request showed here agrees with what was read. {_WALKED}"),
        ]
    ]
    evidence: NotRequired[Annotated[list[EvidencePart], Describe(f"What the request carried at the hop. {_WALKED}")]]
    layers: NotRequired[Annotated[list[LayerPart], Describe(f"The admission layers decided at the hop. {_WALKED}")]]


# ----- The estate -------------------------------------------------------------


class EstateFigure(TypedDict):
    id: Annotated[str, Describe("The figure's stable id, such as `hq.estate.machines`.")]
    label: Annotated[str, Describe("What it counts, as the dashboard heads it.")]
    value: Annotated[str, Describe("The figure as a page shows it, such as `3` or `24 days`.")]
    detail: Annotated[str, Describe("A line under it, such as how many are offline; empty for none.")]
    status: Annotated[str, Describe("`attention` when it needs a look; empty otherwise.")]
    url: Annotated[str, Describe("The page that lists what it counts, or of the one thing it is about.")]


class ActionItemRow(TypedDict):
    status: Annotated[str, Describe(ended(f"How urgent it is: {listed(ATTENTION_ORDER)}"))]
    source: Annotated[str, Describe("The part of HQ it comes from, in words, such as `Containers`.")]
    label: Annotated[str, Describe("What needs doing, in a line.")]
    detail: Annotated[str, Describe("More on it: what was read, and what to do.")]
    action: Annotated[str, Describe("The button that opens it, such as `Open containers`; empty for none.")]
    url: Annotated[str, Describe("The page where it is dealt with.")]
    since: Since


class RolePart(TypedDict):
    id: Annotated[str, Describe("The role's stable id, such as `tailnet-dns`.")]
    label: Annotated[str, Describe("The role, in words.")]


class MachineRow(TypedDict):
    name: Annotated[str, Describe("The machine's name, which a read of one names it by.")]
    url: Annotated[str, Describe("The machine's page in HQ.")]
    state: Annotated[
        str,
        Describe(
            "Whether it can be reached now, such as `online`, `away` for a personal device that is off, "
            "`offline` or `not monitored`."
        ),
    ]
    role: Annotated[
        str, Describe("The role HQ's record of the machine gives it, such as `Docker host`; empty for none.")
    ]
    roles: Annotated[
        list[RolePart], Describe("What the machine does for the rest of the network, as derived from what was read.")
    ]
    addresses: Annotated[
        list[str], Describe("Every address HQ knows reaches the machine, with a port where one is part of it.")
    ]
    hostnames: Annotated[list[str], Describe("The hostnames served from the machine.")]
    runs_hq: Annotated[bool, Describe("Whether HQ itself runs on the machine.")]


class HeaderAddressPart(TypedDict):
    label: Annotated[str, Describe("Which network the address is on, such as `Tailnet`, `LAN` or `Public`.")]
    address: Annotated[str, Describe("The address you reach the machine at there.")]
    holder: Annotated[
        str, Describe("Who holds a public address, such as the company that hosts it; empty for any other.")
    ]


class MachineContainerPart(TypedDict):
    name: Annotated[str, Describe("The container's name, as Docker reports it.")]
    stack: ComposeProject
    image: Annotated[str, Describe("The image reference it runs.")]
    state: Annotated[str, Describe("Docker's state for the container, such as `running`.")]
    status: Annotated[str, Describe("Docker's status line for the container.")]
    published: Annotated[str, Describe("The ports it publishes, as Docker lists them; empty for none.")]
    watcher: Annotated[str, Describe("The key of HQ's record that watches it; empty for none.")]
    observed_at: ObservedAt | None


class PresencePart(TypedDict):
    online: Annotated[bool, Describe("Whether the tailnet says it is online now.")]
    last_seen: Annotated[str | None, Describe("When the tailnet last saw it, as reported; null when not said.")]
    tailnet_name: Annotated[str, Describe("What the tailnet calls it.")]
    dns_name: Annotated[str, Describe("Its MagicDNS name.")]
    tailnet_address: Annotated[str, Describe("Its address on the tailnet.")]
    lan_address: Annotated[str, Describe("The LAN address its client reports; empty for none.")]
    os: Annotated[str, Describe("The operating system its client reports.")]
    client_version: Annotated[str, Describe("The version of the tailnet client it runs.")]
    update_available: Annotated[bool, Describe("Whether a newer client is published for it.")]
    key_expires: Annotated[str | None, Describe("When its node key runs out, as reported; null when it does not.")]
    authorized: Annotated[bool, Describe("Whether the tailnet has authorized it; one it has not reaches nothing.")]
    tags: Annotated[list[str], Describe("The tags it carries.")]
    advertised_routes: Annotated[list[str], Describe("The routes it offers the tailnet.")]
    enabled_routes: Annotated[list[str], Describe("Those of its routes the tailnet has approved, which alone work.")]
    offers_exit_node: Annotated[bool, Describe("Whether it offers to be an exit node.")]
    exit_node_approved: Annotated[bool, Describe("Whether the tailnet has approved it as an exit node.")]
    ssh_enabled: Annotated[bool, Describe("Whether Tailscale SSH is on for it.")]
    blocks_incoming: Annotated[bool, Describe("Whether it accepts no inbound connection at all.")]
    observed_at: ObservedAt | None


class MachineRecord(MachineRow):
    """One machine whole: how it is reached, what it serves and what runs on it."""

    node: Annotated[str, Describe("The machine's id on the map: `machine:` and its name.")]
    declaration: Annotated[str, Describe("The key of HQ's record of the machine; empty when HQ keeps none.")]
    aliases: Annotated[list[str], Describe("Other names the same machine is known by.")]
    header_addresses: Annotated[
        list[HeaderAddressPart], Describe("The addresses you reach it at, tailnet then LAN then public.")
    ]
    containers_visible: Annotated[
        bool,
        Describe("Whether any connected credential reads containers, so an empty `containers` means none run there."),
    ]
    hq_hostnames: Annotated[list[str], Describe("The hostnames HQ answers at on the machine; empty unless `runs_hq`.")]
    reached_by: Annotated[list[str], Describe("The connections HQ reaches it through, by ref.")]
    opened_by: Annotated[
        list[str], Describe("Those of `reached_by` whose credential opens a shell or a Docker environment on it.")
    ]
    unanswered: Annotated[list[str], Describe("Those of `reached_by` that did not answer their last check.")]
    resources: Annotated[list[str], Describe("The keys of HQ's records on the machine.")]
    containers: Annotated[list[MachineContainerPart], Describe("The containers last read on it.")]
    presence: Annotated[
        PresencePart | None, Describe("What the tailnet reports of it; null when the tailnet does not know it.")
    ]


class ContainerRow(TypedDict):
    address: Annotated[str, Describe("`machine:container`, which a read of one names it by.")]
    machine: Annotated[str, Describe("The name of the machine it runs on.")]
    name: Annotated[str, Describe("The container's name.")]
    compose_project: ComposeProject
    url: Annotated[str, Describe("Its page in HQ: its record's when HQ watches it, else its machine's.")]
    state: Annotated[str, Describe("Docker's state for it, such as `running` or `exited`.")]
    status: Annotated[str, Describe("Docker's status line for it, such as `Up 12 days (healthy)`.")]
    serves: Annotated[list[str], Describe("The hostnames it serves.")]


class ProjectPart(TypedDict):
    name: Annotated[str, Describe("The project's name.")]
    slug: Annotated[str, Describe("The project's slug, which its page is addressed by.")]
    url: Annotated[str, Describe("The project's page in HQ.")]


class PullRequestPart(TypedDict):
    number: Annotated[int | None, Describe("The pull request's number; null when not read.")]
    title: Annotated[str, Describe("The pull request's title.")]
    url: Annotated[str, Describe("The pull request's page.")]
    draft: Annotated[bool, Describe("Whether it is a draft.")]


class RepositoryPart(TypedDict):
    name: Annotated[str, Describe("The repository's name, `owner/name`.")]
    url: Annotated[str, Describe("The repository's page.")]
    default_branch: Annotated[str, Describe("The repository's default branch.")]
    head: Annotated[str, Describe("The commit the default branch is at; empty when not read.")]
    deployed: Annotated[str, Describe("The commit production runs, by its last deployment; empty when not read.")]
    undeployed: Annotated[bool, Describe("Whether the default branch is ahead of what production runs.")]
    verified: Annotated[bool, Describe("Whether the checks after its last deployment confirmed production.")]
    open_pull_requests: Annotated[list[PullRequestPart], Describe("Its open pull requests.")]


class UsagePart(TypedDict):
    memory_bytes: Annotated[int, Describe("The memory it uses, in bytes.")]
    memory_counted_as: Annotated[str, Describe("How `memory_bytes` was counted, as Docker reports it.")]
    memory_limit_bytes: Annotated[int | None, Describe("Its memory limit, in bytes; null for none.")]
    memory_of_limit_percent: Annotated[
        float | None, Describe("How much of its memory limit it uses, a percentage; null without a limit.")
    ]
    cpu_percent_of_one_core: Annotated[
        float | None,
        Describe(
            "The CPU it used since the read before, as a percentage of one core. Null until two reads "
            "have sampled one run of it."
        ),
    ]
    cpu_limit: Annotated[float | None, Describe("How many cores it may use; null for no limit.")]
    read_at: Annotated[str | None, Describe("When its use was read, an ISO 8601 date-time; null when not said.")]


class ContainerRecord(ContainerRow):
    """One running container whole: its image and standing, how it runs and what it uses."""

    image: Annotated[
        dict[str, Any],
        Describe(
            "What it runs: reference, registry, repository, tag, digest, image id, whether pinned by digest, "
            "build time and source."
        ),
    ]
    standing: Annotated[
        dict[str, Any],
        Describe(
            "Whether what it runs is current and safe: its state and summary, newer tags, the latest release "
            "and advisories. It also says when the registry and upstream were last read."
        ),
    ]
    project: Annotated[ProjectPart | None, Describe("The project it belongs to; null for none.")]
    repository: Annotated[
        RepositoryPart | None,
        Describe("The repository whose pipeline deploys it; null when none deploys it."),
    ]
    runtime: Annotated[
        AsRead | None, Describe("How it is run, as Docker's inspect reports it; null when that was not read.")
    ]
    usage: Annotated[
        UsagePart | None, Describe("The memory and CPU it uses beside its limits; null when that was not read.")
    ]
    posture: Annotated[
        dict[str, Any],
        Describe(
            "How it meets the container standard: the checks met and each check's state. An unmet check "
            "has its reason and fix; a check off by design says so."
        ),
    ]
    supply_chain: Annotated[
        dict[str, Any],
        Describe(
            "Where its image comes from: source, provenance, packages, SBOM, attestations, and known "
            "vulnerabilities with when they were read. It also says how it meets the supply chain standard."
        ),
    ]


class ImageAt(TypedDict):
    tag: Annotated[str, Describe("The image's tag.")]
    digest: Annotated[str, Describe("The image's digest, `sha256:` and its hash; empty when not read yet.")]


class BlockerPart(TypedDict):
    id: Annotated[str, Describe("What stands in the way, as a stable id an agent can act on, such as `mounts-unread`.")]
    reason: Annotated[str, Describe("What stands in the way, in a sentence.")]


UpgradeRow = TypedDict(
    "UpgradeRow",
    {
        "address": Annotated[str, Describe("The container's `machine:container`, which a read of one names it by.")],
        "viable": Annotated[bool, Describe("Whether nothing blocks it: true when `blockers` is empty.")],
        "risk": Annotated[
            str, Describe(ended(f"How likely the upgrade is to go wrong: {listed((LOW, MEDIUM, HIGH))}"))
        ],
        "change": Annotated[
            str,
            Describe(
                ended(
                    "The size of the version change, by the first number that differs: "
                    f"{listed((PATCH, MINOR, MAJOR, UNKNOWN_CHANGE))}"
                )
            ),
        ],
        "from": Annotated[ImageAt, Describe("The image it runs now.")],
        "to": Annotated[ImageAt, Describe("The image the upgrade would move it to.")],
        "blockers": Annotated[list[BlockerPart], Describe("Each reason it cannot go ahead yet; empty when none.")],
    },
)


class ReleasePart(TypedDict):
    tag: Annotated[str, Describe("The release's tag.")]
    url: Annotated[str, Describe("The release's page.")]
    published: Annotated[str | None, Describe("When it was published, an ISO 8601 date-time; null when not said.")]


class AdvisoryPart(TypedDict):
    id: Annotated[str, Describe("The advisory's or vulnerability's id, such as a GHSA or CVE id.")]
    severity: Annotated[str, Describe("How serious it is, as its source rates it.")]
    summary: Annotated[str, Describe("What the advisory warns of, in a line.")]
    url: Annotated[str, Describe("Its page.")]


class MountPart(TypedDict):
    type: Annotated[str, Describe("What sort of mount it is, such as `bind` or `volume`.")]
    source: Annotated[str, Describe("Where its data is kept on the machine, or the volume's name.")]
    destination: Annotated[str, Describe("Where it is mounted in the container.")]
    read_only: Annotated[bool, Describe("Whether the container may only read it.")]


class UpgradeStepPart(TypedDict):
    id: Annotated[str, Describe("The step's stable id.")]
    label: Annotated[str, Describe("The step, in a few words.")]
    detail: Annotated[str, Describe("What the step does, in a sentence.")]


class UpgradeRecord(UpgradeRow):
    """One upgrade whole: what it fixes and introduces, its data, and the steps it would take."""

    release: Annotated[ReleasePart | None, Describe("The release of the version it moves to; null for none.")]
    fixes: Annotated[
        list[AdvisoryPart],
        Describe("The advisories and vulnerabilities against what runs that the new version is clear of."),
    ]
    introduces: Annotated[list[AdvisoryPart], Describe("Any serious vulnerability the new version would bring.")]
    stateful: Annotated[bool, Describe("Whether it keeps data in writable mounts, which `data` lists.")]
    data: Annotated[
        list[MountPart],
        Describe("The writable mounts holding its data, snapshotted before the upgrade and restored on rollback."),
    ]
    verified_by: Annotated[
        list[str],
        Describe("What would show the new version works, such as its health check; empty when nothing would."),
    ]
    not_automatic: Annotated[
        list[BlockerPart],
        Describe(
            "Why it would not apply on its own, though an operator may approve it. Every blocker comes first, "
            "then the bar a patch must clear."
        ),
    ]
    steps: Annotated[list[UpgradeStepPart], Describe("The steps an upgrade would take, in order.")]


class ZoneMemberPart(TypedDict):
    hostname: Annotated[str, Describe("The hostname of a service under the domain.")]
    status: Annotated[str, Describe(ended(f"How that service stands: {listed(STANDINGS)}"))]


class Registration(TypedDict):
    """The domain's registration as last read: when it runs out and whether it renews."""

    expires: Annotated[str, Describe("When the registration runs out, an ISO 8601 date-time.")]
    source: Annotated[str, Describe("The reading the date comes from, in words.")]
    auto_renew: Annotated[bool, Describe("Whether the registrar renews it on its own.")]


class DomainRow(TypedDict):
    name: Annotated[str, Describe("The domain's name, which a read of one names it by.")]
    node: Annotated[str, Describe("The domain's id on the map: `zone:` and its name.")]
    url: Annotated[str, Describe("The domain's page in HQ.")]
    declaration: Annotated[str, Describe("The key of HQ's record of the domain's DNS zone; empty when HQ keeps none.")]
    services: Annotated[list[ZoneMemberPart], Describe("The services under the domain, HQ's own among them.")]
    registration: Registration | None


class DnsRecordPart(TypedDict):
    name: Annotated[str, Describe("The name in the zone the record is for.")]
    record_type: Annotated[str, Describe("The record's type, such as `A`, `CNAME` or `TXT`.")]
    value: Annotated[str, Describe("What the record holds, as the zone lists it.")]
    proxied: Annotated[bool, Describe("Whether the DNS service proxies traffic for it.")]
    ttl: Annotated[int, Describe("How long a resolver may keep it, in seconds; 1 leaves it to the DNS service.")]
    declaration: Annotated[str, Describe("The key of HQ's record of it; empty for none.")]
    observed_only: Annotated[bool, Describe("Whether only the live zone holds it, with no record of HQ's.")]


class InsightPart(TypedDict):
    label: Annotated[str, Describe("What the insight is about, in a few words.")]
    value: Annotated[str, Describe("What HQ found, in a few words.")]
    detail: Annotated[str, Describe("More on it, in a sentence; empty for none.")]
    url: Annotated[str, Describe("Where to look into it; empty for none.")]
    concern: Annotated[bool, Describe("Whether it is a concern to act on.")]
    notice: Annotated[bool, Describe("Whether it is a notice worth knowing, short of a concern.")]


class DomainRecord(DomainRow):
    """One domain with its records, its connection and what HQ makes of it."""

    connection_ref: Annotated[str, Describe("The connection that reads the domain's DNS records; empty for none.")]
    reachable: Annotated[bool, Describe("Whether that connection answered its last read.")]
    observed_at: ObservedAt | None
    records: Annotated[list[DnsRecordPart], Describe("The domain's DNS records as last read.")]
    insights: Annotated[list[InsightPart], Describe("What HQ makes of the domain.")]


class RelationshipPart(TypedDict):
    entity: WrittenLink
    source: Annotated[WrittenLink | None, Describe("The reading it came from; null when nothing says.")]
    observed_at: ObservedAt | None
    stale: Annotated[bool, Describe("Whether the reading it came from is out of date.")]


class RelationGroupPart(TypedDict):
    phrase: Annotated[str, Describe("What each relationship in it says from the node, such as `runs on`.")]
    rank: Annotated[int, Describe("Where the group falls in page order, lowest first.")]
    items: Annotated[list[RelationshipPart], Describe("The other end of each relationship it holds.")]


class JoinedReadingPart(TypedDict):
    kind: Annotated[str, Describe("The reading's type, such as `adguard.client`.")]
    label: Annotated[str, Describe("The reading's name, as a page heads it.")]
    connection: Annotated[str, Describe("The connection it was read through; empty for none.")]
    records: Annotated[list[AsRead], Describe("The records it holds about the node, as its type's schema admits them.")]


class RelationshipsRecord(TypedDict):
    """One node's edges, grouped by what each says from it, and the readings that hold it."""

    node: Annotated[str, Describe("The id of the node asked about, such as `machine:<name>` or `zone:<domain>`.")]
    focus_url: Annotated[str, Describe("The map centered on the node.")]
    groups: Annotated[list[RelationGroupPart], Describe("Its relationships, grouped by what each says from the node.")]
    readings: Annotated[list[JoinedReadingPart], Describe("The readings joined to the node.")]
    unreadable: Annotated[list[str], Describe("The readings about the node that could not be read, in words.")]


class Problem(TypedDict):
    """One open problem about HQ itself."""

    key: Annotated[
        str, Describe("What the problem is about, stable while it is the same matter, such as `approval:<id>`.")
    ]
    label: Annotated[str, Describe("What is wrong with HQ, in a line.")]
    detail: Annotated[str, Describe("More on the problem: what was read, and what to do.")]
    status: Annotated[str, Describe(ended(f"How serious the problem is: {listed(ATTENTION_ORDER)}"))]
    url: Annotated[str, Describe("Where the problem is dealt with in HQ.")]
    subject: WrittenAction | None
    since: Since


class ReadingRow(TypedDict):
    kind: Annotated[str, Describe("The reading's type, such as `adguard.client`, which a read of one names it by.")]
    label: Annotated[str, Describe("The reading's name, in words.")]
    provider: Annotated[str, Describe("The service it is read from, such as `adguard`.")]
    state: Annotated[str, Describe(ended(f"How its last read went: {listed(STATE_LABELS)}"))]
    records: Annotated[int, Describe("How many records the last read stored.")]
    observed_at: ObservedAt | None
    error: Annotated[str, Describe("Why the last read failed, as the service said; empty when it did not.")]


class ReadingItem(ReadingRow):
    provider_label: Annotated[str, Describe("The service's name, in words.")]
    read_by: Annotated[
        str,
        Describe(
            f"Who takes the reading, {listed(READERS)}: a controller through a connection's credential, HQ from "
            "a public registry, or HQ from the requests it serves."
        ),
    ]
    facet: Annotated[
        str, Describe(f"What its records supply a service: {listed(sorted(READING_FACETS))}, or empty for none.")
    ]
    relation: Annotated[
        str,
        Describe(
            "What a record is to what it joins, a short phrase such as `Served by Pages project`; empty for none."
        ),
    ]
    requires: Annotated[list[str], Describe("The permissions the read needs, each as the service names it.")]
    refusal: Annotated[
        str,
        Describe(
            f"Whether the service refused the credential or one permission, {listed(REFUSALS)}; empty when neither."
        ),
    ]
    remedy: Annotated[
        str, Describe("What would let HQ read it, such as the permissions to add; empty when nothing is missing.")
    ]


class ReadingRecord(ReadingItem):
    """One type of reading with the records it holds, as its schema admits them."""

    items: Annotated[
        list[AsRead],
        Describe("The records it holds, each as its type's schema admits it; empty for a type HQ withholds."),
    ]


class Match(TypedDict):
    """One search match: a machine, service or domain, or a record."""

    kind: Annotated[
        str, Describe("What sort of match it is, such as `machine`, `service` or `zone`, or the list a record is in.")
    ]
    name: Annotated[str, Describe("Its id where it is listed, such as a machine's name, a hostname or a record's key.")]
    label: Annotated[str, Describe("What it is called, as a result shows it.")]
    summary: Annotated[str, Describe("A line on it, with the text that matched.")]
    url: Annotated[str, Describe("Its page in HQ.")]
    group: Annotated[str, Describe("The heading a list of results files it under.")]


# ----- Documentation ---------------------------------------------------------


class DocumentationRow(TypedDict):
    """One documentation pointer; a path or note it may not show is blank."""

    doc_id: DocId
    title: DocumentationTitle
    doc_type: DocType
    system: Annotated[str, Describe("The system or service it is about; empty for none.")]
    environment: DocEnvironment
    status: DocStatus
    sensitivity: DocSensitivity
    obsidian_path: ObsidianPath
    github_path: GithubPath
    external_url: ExternalUrl
    last_reviewed: LastReviewed
    published_at: PublishedAt
    notes: Notes
    updated_at: UpdatedAt


__all__ = [
    "ActionItemRow",
    "ContainerRecord",
    "ContainerRow",
    "DocumentationRow",
    "DomainRecord",
    "DomainRow",
    "EstateFigure",
    "HopRow",
    "MachineRecord",
    "MachineRow",
    "ManagedResourceRecord",
    "ManagedResourceRow",
    "Match",
    "PathRecord",
    "Problem",
    "ReadingRecord",
    "ReadingRow",
    "RelationshipsRecord",
    "ServiceRecord",
    "ServiceRow",
    "UpgradeRecord",
    "UpgradeRow",
]
