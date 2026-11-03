"""The shapes the host's own resources answer in: each list's row and each record.

A row is what a list read keeps of each item unless the caller names its own
fields, so its keys are the kind's ``list_fields``; a record is what a read of
one answers, and extends the row where it is the item itself. The API document
types each read by them, and a test holds every answer on the showcase data to
its type. A part these leave as an object is typed where it is built, or not
yet.

Each field says what it holds where it is declared, and a closed set of values
is named from the constant that holds it (``describe.listed``).
"""

from typing import Annotated, Any, NotRequired, TypedDict

from hq.domains.control_plane.observations.contract import READERS, READING_FACETS
from hq.domains.control_plane.provider_adapters.contracts import REFUSALS

from .answer_fields import ObservedAt, Since, UpdatedAt
from .connection_catalog import AUTHORITY_LABELS
from .connection_contracts import CREDENTIAL_MODELS
from .connection_lifecycle import Lifecycle
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
from .plugins import ATTENTION_ORDER
from .record_fields import Notes, PublishedAt
from .service_health import ATTENTION, GOOD, SERIOUS, UNKNOWN
from .services import HQ_MARK, OBSERVED_MARK
from .topology_model import STANDINGS
from .ui import ended
from .upgrades import HIGH, LOW, MAJOR, MEDIUM, MINOR, PATCH, UNKNOWN_CHANGE
from .workflows import WrittenAction

# ----- Records HQ manages -----------------------------------------------------


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
    conditions: Annotated[
        list[dict[str, Any]],
        Describe(
            "Its conditions as last reported, each a type such as `Ready` or `Degraded`, whether it holds, "
            "a reason and a message."
        ),
    ]
    generation: Annotated[int, Describe("Which version of HQ's copy this is, counting up from 1 as it changes.")]
    observed_generation: Annotated[
        int,
        Describe(
            "Which version of HQ's copy was last applied and confirmed back; it equals the current "
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
            "What HQ makes of the record: its health, what may be done to it and why not, its machines and "
            "services, what HQ expects beside what is live, a certificate's expiry and use, the changes waiting "
            "for approval, and what uses it."
        ),
    ]
    operations: Annotated[list[dict[str, Any]], Describe("The latest operations that changed it, newest first.")]


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
        list[dict[str, Any]],
        Describe(
            "Each part the service is made of, such as its DNS record, proxy, certificate and container: "
            "whether it is there, its state, and the keys of HQ's records behind it."
        ),
    ]


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
        list[dict[str, Any]],
        Describe(
            "The readings that name the hostname as something that answers, each its relation, name, link "
            "and where it was read."
        ),
    ]
    routes: Annotated[
        list[dict[str, Any]],
        Describe(
            "Each route a request can take, from one DNS answer to what serves the name: what it starts from, "
            "the port of a forward, its line and its hops."
        ),
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
    link: Annotated[dict[str, Any] | None, Describe("The page of what the hop names; null when it has none.")]
    detail: Annotated[
        str, Describe("A line on what it does there, such as where it forwards; empty when there is nothing to add.")
    ]
    source: Annotated[
        dict[str, Any] | None,
        Describe(
            "Where HQ read the hop: the reading's type, label, connection and when, or HQ's own record; "
            "null when nothing says."
        ),
    ]
    certificate: Annotated[
        dict[str, Any] | None,
        Describe(
            "The certificate the hop presents, its name, issuer and expiry and where it was read, or why HQ "
            "cannot say; null when it presents none."
        ),
    ]
    unread: Annotated[str, Describe("What HQ could not read about the hop, a sentence; empty when nothing is missing.")]
    overlays: Annotated[
        list[dict[str, Any]],
        Describe("What sits over the hop, such as an access gate, each a relation and a link."),
    ]
    check: NotRequired[
        Annotated[
            dict[str, Any],
            Describe(
                f"Whether what the request showed here agrees with what was read: a state, label and detail. {_WALKED}"
            ),
        ]
    ]
    evidence: NotRequired[
        Annotated[
            list[dict[str, Any]],
            Describe(f"What the request carried at the hop, each a label, value, detail and role. {_WALKED}"),
        ]
    ]
    layers: NotRequired[
        Annotated[
            list[dict[str, Any]],
            Describe(
                "The admission layers decided at the hop, each with its state, detail, boundary, mechanism "
                f"and rules. {_WALKED}"
            ),
        ]
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


class MachineRecord(MachineRow):
    """One machine whole: how it is reached, what it serves and what runs on it."""

    node: Annotated[str, Describe("The machine's id in the topology: `machine:` and its name.")]
    declaration: Annotated[str, Describe("The key of HQ's record of the machine; empty when HQ keeps none.")]
    aliases: Annotated[list[str], Describe("Other names the same machine is known by.")]
    header_addresses: Annotated[
        list[dict[str, Any]],
        Describe(
            "The addresses a person reaches it at, tailnet then LAN then public, each a label, the address "
            "and who holds a public one."
        ),
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
    containers: Annotated[
        list[dict[str, Any]],
        Describe(
            "The containers last read on it, each its name, compose project, image, state, status, what it "
            "publishes, the key of HQ's record that watches it, and when it was read."
        ),
    ]
    presence: Annotated[
        dict[str, Any] | None,
        Describe(
            "What the tailnet reports of it: whether it is online, its names and addresses, OS and client, "
            "key expiry, tags, routes, exit node and SSH settings, and when that was read; null when the "
            "tailnet does not know it."
        ),
    ]


class ContainerRow(TypedDict):
    address: Annotated[str, Describe("`machine:container`, which a read of one names it by.")]
    machine: Annotated[str, Describe("The name of the machine it runs on.")]
    name: Annotated[str, Describe("The container's name.")]
    compose_project: Annotated[str, Describe("The compose project it belongs to; empty for one started on its own.")]
    url: Annotated[str, Describe("Its page in HQ: its record's when HQ watches it, else its machine's.")]
    state: Annotated[str, Describe("Docker's state for it, such as `running` or `exited`.")]
    status: Annotated[str, Describe("Docker's status line for it, such as `Up 12 days (healthy)`.")]
    serves: Annotated[list[str], Describe("The hostnames it serves.")]


class ContainerRecord(ContainerRow):
    """One running container whole: its image and standing, how it runs and what it uses."""

    image: Annotated[
        dict[str, Any],
        Describe(
            "What it runs: the reference, registry, repository, tag, digest and image id, whether it is pinned "
            "by digest, when it was built, and its source."
        ),
    ]
    standing: Annotated[
        dict[str, Any],
        Describe(
            "Whether what it runs is current and safe: a state and summary, newer tags, the latest release, "
            "advisories, and when the registry and upstream were last read."
        ),
    ]
    project: Annotated[
        dict[str, Any] | None, Describe("The project it belongs to, a name, slug and page; null for none.")
    ]
    repository: Annotated[
        dict[str, Any] | None,
        Describe(
            "The repository whose pipeline deploys it: its default branch, head and deployed commits, whether "
            "production was verified, and its open pull requests; null when none deploys it."
        ),
    ]
    runtime: Annotated[
        dict[str, Any] | None, Describe("How it is run, from Docker's inspect; null when that was not read.")
    ]
    usage: Annotated[
        dict[str, Any] | None,
        Describe("The memory and CPU it uses beside its limits, and when that was read; null when it was not."),
    ]
    posture: Annotated[
        dict[str, Any],
        Describe(
            "How it measures against the container standard: how many checks it meets of those measured, each "
            "check's state, why each unmet one matters and its fix, and what is off by design."
        ),
    ]
    supply_chain: Annotated[
        dict[str, Any],
        Describe(
            "Where its image comes from: its source, provenance, packages, SBOM, attestations, known "
            "vulnerabilities and when they were read, and how it measures against the supply chain standard."
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


class UpgradeRecord(UpgradeRow):
    """One upgrade whole: what it fixes and introduces, its data, and the steps it would take."""

    release: Annotated[
        dict[str, Any] | None,
        Describe("The release of the version it moves to: its tag, page and when it was published; null for none."),
    ]
    fixes: Annotated[
        list[dict[str, Any]],
        Describe("The advisories and vulnerabilities against what runs that the new version is clear of."),
    ]
    introduces: Annotated[list[dict[str, Any]], Describe("Any serious vulnerability the new version would bring.")]
    stateful: Annotated[bool, Describe("Whether it keeps data in writable mounts, which `data` lists.")]
    data: Annotated[
        list[dict[str, Any]],
        Describe("The writable mounts holding its data, snapshotted before the upgrade and restored on rollback."),
    ]
    verified_by: Annotated[
        list[str],
        Describe("What would show the new version works, such as its health check; empty when nothing would."),
    ]
    not_automatic: Annotated[
        list[BlockerPart],
        Describe(
            "Why it would not apply on its own, though a person may approve it: every blocker, then the bar a patch must clear."
        ),
    ]
    steps: Annotated[
        list[dict[str, Any]], Describe("The steps an upgrade would take, in order, each an id, label and detail.")
    ]


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
    node: Annotated[str, Describe("The domain's id in the topology: `zone:` and its name.")]
    url: Annotated[str, Describe("The domain's page in HQ.")]
    declaration: Annotated[str, Describe("The key of HQ's record of the domain's DNS zone; empty when HQ keeps none.")]
    services: Annotated[list[ZoneMemberPart], Describe("The services under the domain, HQ's own among them.")]
    registration: Registration | None


class DomainRecord(DomainRow):
    """One domain with its records, its connection and what HQ makes of it."""

    connection_ref: Annotated[str, Describe("The connection that reads the domain's DNS records; empty for none.")]
    reachable: Annotated[bool, Describe("Whether that connection answered its last read.")]
    observed_at: ObservedAt | None
    records: Annotated[
        list[dict[str, Any]],
        Describe(
            "The domain's DNS records as last read, each its name, record type, value, whether it is proxied, "
            "its TTL in seconds, the key of HQ's record of it (empty for none), and whether only the live "
            "zone holds it."
        ),
    ]
    insights: Annotated[
        list[dict[str, Any]],
        Describe(
            "What HQ makes of the domain, each a label, value, detail and link, and whether it is a concern or a notice."
        ),
    ]


class RelationshipsRecord(TypedDict):
    """One node's edges, grouped by what each says from it, and the readings that hold it."""

    node: Annotated[str, Describe("The id of the node asked about, such as `machine:<name>` or `zone:<domain>`.")]
    focus_url: Annotated[str, Describe("The topology page centered on the node.")]
    groups: Annotated[
        list[dict[str, Any]],
        Describe(
            "Its relationships under what each says from the node (`phrase`), in page order (`rank`): each item "
            "the other end as a link, the reading it came from, when that was read, and whether it is out of date."
        ),
    ]
    readings: Annotated[
        list[dict[str, Any]],
        Describe(
            "The readings joined to the node, each its type, label, connection and the records it holds about it."
        ),
    ]
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
        list[dict[str, Any]],
        Describe("The records it holds, each as its type's schema admits it; empty for a type HQ withholds."),
    ]


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


class CredentialRecord(CredentialRow):
    """What one service's credential reads, each reading or listing it sees."""

    manages: Annotated[list[str], Describe("The types of record its credential changes, in words.")]
    sights: Annotated[
        list[dict[str, Any]],
        Describe(
            "Each reading or record type it could read, with its state, records, when it was read, the error, "
            "the permissions it needs and what would fix it."
        ),
    ]


class Tailnet(TypedDict):
    """The tailnet's policy and what HQ makes of it."""

    known: Annotated[bool, Describe("Whether the policy has been read: true once it holds a group, tag or grant.")]
    declaration: Annotated[
        str | None, Describe("The key of HQ's record of the policy, which a change to it edits; null when none.")
    ]
    counts: Annotated[dict[str, int], Describe("How many grants, groups, tags and tests the policy holds.")]
    settings: Annotated[
        list[dict[str, Any]],
        Describe("The tailnet's settings, each a label and value, with the machines a value names."),
    ]
    grants: Annotated[
        list[dict[str, Any]],
        Describe(
            "The policy's grants as written, each with the machines its sources and destinations name, and its ports."
        ),
    ]
    ssh_rules: Annotated[
        list[dict[str, Any]],
        Describe("The policy's SSH rules as written, each with the machines its sources and destinations name."),
    ]
    groups: Annotated[list[dict[str, Any]], Describe("The policy's groups, each a name and its members.")]
    tags: Annotated[
        list[dict[str, Any]], Describe("The policy's tags, each a name, its owners and the machines that carry it.")
    ]
    services: Annotated[list[dict[str, Any]], Describe("The tailnet's services, as read.")]
    app_connectors: Annotated[list[dict[str, Any]], Describe("The tailnet's app connectors, as read.")]
    tests: Annotated[list[dict[str, Any]], Describe("The policy's tests, as written.")]
    locked_out: Annotated[list[str], Describe("The machines tailnet lock keeps off the tailnet.")]
    findings: Annotated[list[dict[str, Any]], Describe("The open problems about the tailnet that the reader may see.")]
    unread: Annotated[
        list[dict[str, Any]],
        Describe("Each tailnet reading that could not be read: its type, label and state, why, and what would fix it."),
    ]
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
    facts: Annotated[list[dict[str, Any]], Describe("What the connection reported of itself, each a label and value.")]
    actions: Annotated[
        list[WrittenAction],
        Describe("What the reader may do to the connection, each already checked against the reader's permissions."),
    ]
    dependencies: Annotated[
        list[dict[str, Any]], Describe("What the connection depends on, each a label, a page and a record's key.")
    ]
    targets: Annotated[
        list[dict[str, Any]], Describe("What the connection points at, each a label, a page and a record's key.")
    ]
    reads: Annotated[
        list[dict[str, Any]],
        Describe("What its credential reads, each a label with what was last read and the permissions behind it."),
    ]
    changes: Annotated[
        list[dict[str, Any]],
        Describe("What its credential may change, each with the permissions it needs and whether it holds them."),
    ]
    family_label: Annotated[str, Describe("The connection type's name, in words.")]
    used_for: Annotated[str | None, Describe("The part of HQ that uses it, as the navigation names it; null for none.")]
    secret_store: Annotated[str | None, Describe("Where its credential is kept; null when its type names no place.")]
    reach: Annotated[
        dict[str, Any] | None,
        Describe(
            "How HQ reaches its address: the network, the host and machine, and on the tailnet the path and "
            "traffic; null when that was not worked out."
        ),
    ]
    sight: Annotated[
        dict[str, Any] | None,
        Describe(
            "What its credential can read: the service, why the service refused the credential, what it changes, "
            "and a count in each state; null when nothing reads through it."
        ),
    ]
    would_also_see: Annotated[
        list[str], Describe("What the credential would also read with more permissions, each naming them.")
    ]
    credential_fix: Annotated[
        dict[str, Any] | None,
        Describe(
            "How to fix its credential: the permissions missing, what they would show, its expiry and the steps; "
            "null when it needs no fix."
        ),
    ]
    last_activity: Annotated[
        dict[str, Any] | None,
        Describe("The latest audit entry about it: a summary, when, and the entry's id; null for none."),
    ]
    read_now: WrittenAction | None
    read_requested_at: Annotated[
        str | None,
        Describe(
            "When a refresh of it was asked for that has not run yet, an ISO 8601 date-time; null when none waits."
        ),
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
    "ConnectionStandingRecord",
    "ConnectionStandingRow",
    "ContainerRecord",
    "ContainerRow",
    "CredentialRecord",
    "CredentialRow",
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
    "Tailnet",
    "UpgradeRecord",
    "UpgradeRow",
]
