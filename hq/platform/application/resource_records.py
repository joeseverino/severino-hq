"""The shapes the host's own resources answer in: each list's item, row and record.

The API document types each read by them, and a test holds every answer on the
showcase data to its type. A part in its source's own shape is ``AsRead``.
"""

from typing import Annotated, Any, NotRequired, TypedDict

from hq.domains.control_plane.observations.contract import READERS, READING_FACETS
from hq.domains.control_plane.provider_adapters.contracts import REFUSALS

from .answer_fields import (
    AsRead,
    DateTime,
    ObservedAt,
    Since,
    UpdatedAt,
)
from .credential_sight import STATE_LABELS
from .describe import Describe, listed
from .entity_links import EntityLink
from .finding_steps import WrittenCheck, WrittenReason, WrittenSteps
from .json_schemas import Written
from .path_model import Certificate, Evidence, HopCheck, HopSource
from .plugins import ATTENTION_ORDER
from .services import HQ_MARK, OBSERVED_MARK
from .standards import INTENDED, Standing
from .tone import Tone
from .topology_model import RelationGroup
from .ui import ended
from .upgrades import HIGH, LOW, MAJOR, MEDIUM, MINOR, PATCH, UNKNOWN_CHANGE, Blocker, UpgradeStep
from .whereabouts import Origin
from .workflows import WrittenAction

# What stands in the way of an upgrade, as ``Blocker`` says it.
WrittenBlocker = Annotated[dict[str, Any], Written(Blocker)]

# What a container row and a machine's list of its containers say alike.
type ComposeProject = Annotated[str, Describe("The compose project it belongs to; empty for one started on its own.")]

# A name and the page it opens, as ``EntityLink`` says it.
WrittenLink = Annotated[dict[str, Any], Written(EntityLink)]

# ----- Services and paths -----------------------------------------------------


class ServiceRow(TypedDict):
    hostname: Annotated[str, Describe("The service's hostname, which a read of one names it by.")]
    status: Annotated[
        str,
        Describe(
            f"How the service stands by its health rule: {listed((Tone.GOOD, Tone.ATTENTION, Tone.SERIOUS, 'unknown'))}, "
            "the last when nothing reads its health."
        ),
    ]
    status_label: Annotated[str, Describe("The service's health in the words its row shows, such as `Working`.")]
    origin: Annotated[dict[str, Any], Written(Origin)] | None
    project: Annotated[str | None, Describe("The name of the project published at this name; null for none.")]
    faults: Annotated[list[str], Describe("What is not set up right, each a sentence; empty when nothing is.")]


class FacetPart(TypedDict):
    id: Annotated[str, Describe("Which part of the service it is, such as `dns`, `proxy`, `certificate` or `runtime`.")]
    label: Annotated[str, Describe("That part in the words its card is headed with, such as `DNS`.")]
    present: Annotated[bool, Describe("Whether any record of HQ's supplies it.")]
    state: Annotated[
        str,
        Describe(
            f"How the records behind it stand: {listed((Tone.GOOD, Tone.ATTENTION, Tone.SERIOUS))}; empty when none supplies it."
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


# Where HQ read a hop, as ``HopSource`` says it.
WrittenSource = Annotated[dict[str, Any], Written(HopSource)]


class OverlayPart(TypedDict):
    relation: Annotated[str, Describe("What it is to the hop, such as an access gate.")]
    link: WrittenLink | None


class CheckPart(TypedDict):
    id: Annotated[str, Describe("The check's stable id, such as `edge` or `tailnet-lock`.")]
    label: Annotated[str, Describe("What is true when it is met, in words.")]
    state: Annotated[
        str,
        Describe(ended(f"Where it stands: {listed((*Standing, INTENDED))}; the last is failed on purpose")),
    ]
    limited: Annotated[bool, Describe("Met, with a limit the reader should see.")]
    serious: Annotated[bool, Describe("Whether failing it lets someone or something gain reach.")]
    detail: Annotated[str, Describe("Why it was decided so, or why it is failed on purpose, in a sentence.")]
    evidence: Annotated[str, Describe("What it was decided from, or why HQ could not read it; empty for none.")]
    why: Annotated[str, Describe("Why it matters, in a sentence.")]
    fix: Annotated[str, Describe("How it is met, in a sentence.")]
    read_with: Annotated[str, Describe("What would let HQ read a check it could not; empty for none.")]
    read_with_url: Annotated[str, Describe("Where that is done; empty for none.")]
    boundary: Annotated[str, Describe("The security boundary it belongs to; empty for none.")]
    mechanism: Annotated[str, Describe("What enforces it; empty for none.")]
    rules: Annotated[list[str], Describe("The rules behind it, where what decides it has rules.")]


class MeasurementPart(TypedDict):
    """One subject against a standard: how many checks it meets, and each check."""

    met: Annotated[int, Describe("How many checks are met.")]
    measured: Annotated[int, Describe("How many checks HQ could read.")]
    unread: Annotated[int, Describe("How many checks HQ could not read.")]
    checks: Annotated[list[CheckPart], Describe("Each check that applies, in the standard's order.")]


class ObservedPart(TypedDict):
    relation: Annotated[str, Describe("What the reading says the hostname is to it, such as `Served by`.")]
    name: Annotated[str, Describe("What answers for the hostname, by name.")]
    link: WrittenLink | None
    detail: Annotated[str, Describe("A line on what it does for the hostname; empty for none.")]
    source: Annotated[WrittenSource | None, Describe("Where HQ read it; null when nothing says.")]


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
    source: Annotated[WrittenSource | None, Describe("Where HQ read the hop; null when nothing says.")]
    certificate: Annotated[
        Annotated[dict[str, Any], Written(Certificate)] | None,
        Describe("The certificate the hop presents, or why HQ cannot say; null when it presents none."),
    ]
    unread: Annotated[str, Describe("What HQ could not read about the hop, a sentence; empty when nothing is missing.")]
    overlays: Annotated[list[OverlayPart], Describe("What sits over the hop, such as an access gate.")]
    check: NotRequired[
        Annotated[
            Annotated[dict[str, Any], Written(HopCheck)],
            Describe(
                "Whether what the request showed here agrees with what was read, its `state` `unavailable` "
                f"where the request does not show it. {_WALKED}"
            ),
        ]
    ]
    evidence: NotRequired[
        Annotated[
            list[Annotated[dict[str, Any], Written(Evidence)]],
            Describe(f"What the request carried at the hop. {_WALKED}"),
        ]
    ]
    layers: NotRequired[
        Annotated[list[CheckPart], Describe(f"The checks of the request standard decided at the hop. {_WALKED}")]
    ]


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


class ActionItem(ActionItemRow):
    """One action item: what needs doing, where, and the calls and commands that do it."""

    key: Annotated[str, Describe("The item's key, which setting it aside names it by.")]
    source_id: Annotated[str, Describe("The stable id of the part of HQ it comes from, such as `containers`.")]
    count: Annotated[int, Describe("How many things it stands for, at least 1.")]
    steps: WrittenSteps
    no_help_reason: WrittenReason
    check: WrittenCheck
    actions: Annotated[
        list[WrittenAction], Describe("The quick actions offered on it, each through its owner's own command or page.")
    ]
    subject: Annotated[
        WrittenAction | None, Describe("The thing it is about, as a link to its own page; null for none.")
    ]
    notice: Annotated[bool, Describe("Whether it only reports something, with nothing to resolve.")]
    observed_at: Annotated[str, Describe("When the reading it is drawn from was taken, in ISO 8601; empty for none.")]
    facts: Annotated[list[list[str]], Describe("What was read that shows it, each a label and its value.")]
    links: Annotated[
        list[WrittenAction], Describe("Pages that help look into it, such as where it sits; never one that acts.")
    ]


class RolePart(TypedDict):
    id: Annotated[str, Describe("The role's stable id, such as `tailnet-dns`.")]
    label: Annotated[str, Describe("The role, in words.")]
    detail: Annotated[str, Describe("What the role means, and what was read that shows it.")]


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
    tone: Annotated[
        str, Describe(ended(f"How its state is colored: {listed((Tone.GOOD, Tone.NEUTRAL, Tone.SERIOUS))}"))
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


class MetricPart(TypedDict):
    label: Annotated[str, Describe("What it measures, such as `CPU`.")]
    value: Annotated[str, Describe("The measure, as a page shows it, such as `12%`.")]
    detail: Annotated[str, Describe("A line under it, such as `4 cores`.")]


class LoadPart(TypedDict):
    metrics: Annotated[
        list[MetricPart],
        Describe("Each measure of the last reading, less a count of containers where `containers` lists them."),
    ]
    observed_at: ObservedAt | None


class ArrivalPart(TypedDict):
    last_seen: Annotated[str, DateTime(), Describe("When a request last came to HQ from it, an ISO 8601 date-time.")]
    how: Annotated[str, Describe("How that request came, such as `directly over the tailnet`.")]


class PeeringPart(TypedDict):
    last_handshake: Annotated[str, Describe("When it and HQ's machine last completed a handshake, as reported.")]
    active: Annotated[bool, Describe("Whether traffic is passing between the two now.")]
    path: Annotated[
        str, Describe("How the two reach each other: `direct`, `relayed`, or `negotiating` before any traffic.")
    ]
    direct_endpoint: Annotated[str, Describe("The address a direct path reaches it at; empty for none.")]
    relay: Annotated[str, Describe("The relay that carries a path with no direct one, or stands by for one.")]
    rx_bytes: Annotated[int, Describe("The bytes HQ's machine has received from it.")]
    tx_bytes: Annotated[int, Describe("The bytes HQ's machine has sent it.")]
    public_key: Annotated[str, Describe("Its node key.")]


class OpeningPart(TypedDict):
    port: Annotated[int, Describe("A port the tailnet policy opens on it.")]
    open_to: Annotated[list[str], Describe("Who the policy lets in at that port, as the policy writes them.")]


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
    external: Annotated[bool, Describe("Whether it belongs to another tailnet, shared into this one.")]
    lock_error: Annotated[str, Describe("Why tailnet lock has not signed it; empty when signed or lock is off.")]
    signs_tailnet_lock: Annotated[bool, Describe("Whether its device is one tailnet lock trusts to sign.")]
    unapproved_routes: Annotated[
        list[str], Describe("Those of its advertised routes the tailnet has not approved, which reach nothing.")
    ]
    observer: Annotated[bool, Describe("Whether it is HQ's own machine, whose tailnet client this reading is from.")]
    reached_hq: Annotated[
        ArrivalPart | None, Describe("The last request that came to HQ from it; null when none came lately.")
    ]
    peering: Annotated[
        PeeringPart | None, Describe("How it and HQ's machine are talking; null until a handshake completes.")
    ]
    openings: Annotated[list[OpeningPart], Describe("Who the tailnet policy lets in, per port.")]
    observed_at: ObservedAt | None


class MachineRecord(MachineRow):
    """One machine whole: how it is reached, what it serves and what runs on it."""

    node: Annotated[str, Describe("The machine's id on the map: `machine:` and its name.")]
    declaration: Annotated[str, Describe("The key of HQ's record of the machine; empty when HQ keeps none.")]
    aliases: Annotated[list[str], Describe("Other names the same machine is known by.")]
    address: Annotated[str, Describe("The address its connection reaches it at; empty when none says.")]
    tailnet_device: Annotated[
        str, Describe("The key of HQ's record of its tailnet device, which approves its routes; empty for none.")
    ]
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
    running: Annotated[int, Describe("How many of `containers` are running.")]
    load: Annotated[LoadPart | None, Describe("Its last reading of load; null when nothing reads it.")]
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
    posture: MeasurementPart
    supply_chain: Annotated[
        dict[str, Any],
        Describe(
            "Where its image comes from: source, provenance, packages, SBOM, attestations, and known "
            "vulnerabilities with when they were read. It also says how it meets the supply chain standard."
        ),
    ]


class ImageAt(TypedDict):
    tag: Annotated[str, Describe("The image's tag.")]
    digest: Annotated[
        str,
        Describe(
            "The image's digest, `sha256:` and its hash; empty when not read yet.",
            examples=("sha256:50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c",),
        ),
    ]


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
        "blockers": Annotated[list[WrittenBlocker], Describe("Each reason it cannot go ahead yet; empty when none.")],
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
        list[WrittenBlocker],
        Describe(
            "Why it would not apply on its own, though an operator may approve it. Every blocker comes first, "
            "then the bar a patch must clear."
        ),
    ]
    steps: Annotated[
        list[Annotated[dict[str, Any], Written(UpgradeStep)]], Describe("The steps an upgrade would take, in order.")
    ]


class ZoneMemberPart(TypedDict):
    hostname: Annotated[str, Describe("The hostname of a service under the domain.")]
    status: Annotated[str, Describe(ended(f"How that service stands: {listed(Tone)}"))]


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


class JoinedReadingPart(TypedDict):
    kind: Annotated[str, Describe("The reading's type, such as `adguard.client`.")]
    label: Annotated[str, Describe("The reading's name, as a page heads it.")]
    connection: Annotated[str, Describe("The connection it was read through; empty for none.")]
    records: Annotated[list[AsRead], Describe("The records it holds about the node, as its type's schema admits them.")]


class RelationshipsRecord(TypedDict):
    """One node's edges, grouped by what each says from it, and the readings that hold it."""

    node: Annotated[str, Describe("The id of the node asked about, such as `machine:<name>` or `zone:<domain>`.")]
    focus_url: Annotated[str, Describe("The map centered on the node.")]
    groups: Annotated[
        list[Annotated[dict[str, Any], Written(RelationGroup)]],
        Describe("Its relationships, grouped by what each says from the node."),
    ]
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


__all__ = [
    "ActionItem",
    "ActionItemRow",
    "ContainerRecord",
    "ContainerRow",
    "DomainRecord",
    "DomainRow",
    "EstateFigure",
    "HopRow",
    "MachineRecord",
    "MachineRow",
    "Match",
    "PathRecord",
    "Problem",
    "ReadingItem",
    "ReadingRecord",
    "ReadingRow",
    "RelationshipsRecord",
    "ServiceItem",
    "ServiceRow",
    "UpgradeRecord",
    "UpgradeRow",
]
