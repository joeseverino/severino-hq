"""The shapes the host's own resources answer in: each list's row and each record.

A row is what a list read keeps of each item unless the caller names its own
fields, so its keys are the kind's ``list_fields``; a record is what a read of
one answers, and extends the row where it is the item itself. The API document
types each read by them, and a test holds every answer on the showcase data to
its type. A part these leave as an object is typed where it is built, or not
yet.
"""

from typing import Any, NotRequired, TypedDict

from .workflows import WrittenAction

# ----- Records HQ manages -----------------------------------------------------


class HealthPart(TypedDict):
    state: str
    label: str
    reason: str
    message: str


class ManagedResourceRow(TypedDict):
    key: str
    kind: str
    enabled: bool
    in_sync: bool
    health: HealthPart
    public_effect: bool
    last_observed_at: str | None


class ManagedResourceItem(ManagedResourceRow):
    """One managed record whole: what HQ expects of it and what was last read."""

    id: str
    spec: dict[str, Any]
    status: dict[str, Any]
    conditions: list[dict[str, Any]]
    generation: int
    observed_generation: int
    updated_at: str


class ManagedResourceRecord(TypedDict):
    """One managed record, what HQ derives from it, and the operations run on it."""

    resource: ManagedResourceItem
    derived: dict[str, Any]
    operations: list[dict[str, Any]]


# ----- Services and paths -----------------------------------------------------


class OriginPart(TypedDict):
    address: str
    host: str
    container: str


class ServiceRow(TypedDict):
    hostname: str
    status: str
    status_label: str
    origin: OriginPart | None
    # The name of the project it belongs to.
    project: str | None
    faults: list[str]


class ServiceItem(ServiceRow):
    """One service name whole, each facet with the records behind it."""

    mark: str
    facets: list[dict[str, Any]]


class PathRecord(TypedDict):
    """The path a request for a hostname takes, hop by hop on each route."""

    hostname: str
    line: str
    redirects_to: str
    machine: str
    unread: list[str]
    observed: list[dict[str, Any]]
    routes: list[dict[str, Any]]


class ServiceRecord(TypedDict):
    """One service name and the path a request for it takes."""

    service: ServiceItem
    path: PathRecord


class HopRow(TypedDict):
    """One hop of HQ's own path; a hop walked for the calling request adds what it showed there."""

    step: str
    label: str
    name: str
    link: dict[str, Any] | None
    detail: str
    source: dict[str, Any] | None
    certificate: dict[str, Any] | None
    unread: str
    overlays: list[dict[str, Any]]
    check: NotRequired[dict[str, Any]]
    evidence: NotRequired[list[dict[str, Any]]]
    layers: NotRequired[list[dict[str, Any]]]


# ----- The estate -------------------------------------------------------------


class EstateFigure(TypedDict):
    id: str
    label: str
    value: str
    detail: str
    status: str
    url: str


class ActionItemRow(TypedDict):
    status: str
    source: str
    label: str
    detail: str
    action: str
    url: str
    since: str


class RolePart(TypedDict):
    id: str
    label: str


class MachineRow(TypedDict):
    name: str
    url: str
    state: str
    role: str
    roles: list[RolePart]
    addresses: list[str]
    hostnames: list[str]
    runs_hq: bool


class MachineRecord(MachineRow):
    """One machine whole: how it is reached, what it serves and what runs on it."""

    node: str
    declaration: str
    aliases: list[str]
    header_addresses: list[dict[str, Any]]
    containers_visible: bool
    hq_hostnames: list[str]
    reached_by: list[str]
    opened_by: list[str]
    unanswered: list[str]
    resources: list[str]
    containers: list[dict[str, Any]]
    presence: dict[str, Any] | None


class ContainerRow(TypedDict):
    address: str
    machine: str
    name: str
    compose_project: str
    url: str
    state: str
    status: str
    serves: list[str]


class ContainerRecord(ContainerRow):
    """One running container whole: its image and standing, how it runs and what it uses."""

    image: dict[str, Any]
    standing: dict[str, Any]
    project: dict[str, Any] | None
    repository: dict[str, Any] | None
    runtime: dict[str, Any] | None
    usage: dict[str, Any] | None
    posture: dict[str, Any]
    supply_chain: dict[str, Any]


class ImageAt(TypedDict):
    tag: str
    digest: str


class BlockerPart(TypedDict):
    id: str
    reason: str


UpgradeRow = TypedDict(
    "UpgradeRow",
    {
        "address": str,
        "viable": bool,
        "risk": str,
        "change": str,
        "from": ImageAt,
        "to": ImageAt,
        "blockers": list[BlockerPart],
    },
)


class UpgradeRecord(UpgradeRow):
    """One upgrade whole: what it fixes and introduces, its data, and the steps it would take."""

    release: dict[str, Any] | None
    fixes: list[dict[str, Any]]
    introduces: list[dict[str, Any]]
    stateful: bool
    data: list[dict[str, Any]]
    verified_by: list[str]
    not_automatic: list[BlockerPart]
    steps: list[dict[str, Any]]


class ZoneMemberPart(TypedDict):
    hostname: str
    status: str


class Registration(TypedDict):
    expires: str
    source: str
    auto_renew: bool


class DomainRow(TypedDict):
    name: str
    node: str
    url: str
    declaration: str
    services: list[ZoneMemberPart]
    registration: Registration | None


class DomainRecord(DomainRow):
    """One domain with its records, its connection and what HQ makes of it."""

    connection_ref: str
    reachable: bool
    observed_at: str | None
    records: list[dict[str, Any]]
    insights: list[dict[str, Any]]


class RelationshipsRecord(TypedDict):
    """One node's edges, grouped by what each says from it, and the readings that hold it."""

    node: str
    focus_url: str
    groups: list[dict[str, Any]]
    readings: list[dict[str, Any]]
    unreadable: list[str]


class Problem(TypedDict):
    """One open problem about HQ itself."""

    key: str
    label: str
    detail: str
    status: str
    url: str
    subject: WrittenAction | None
    since: str


class ReadingRow(TypedDict):
    kind: str
    label: str
    provider: str
    state: str
    records: int
    observed_at: str | None
    error: str


class ReadingItem(ReadingRow):
    provider_label: str
    read_by: str
    facet: str
    relation: str
    requires: list[str]
    refusal: str
    remedy: str


class ReadingRecord(ReadingItem):
    """One type of reading with the records it holds, as its schema admits them."""

    items: list[dict[str, Any]]


class TallyPart(TypedDict):
    count: int
    state: str


class CredentialRow(TypedDict):
    provider: str
    label: str
    credential_refusal: str
    tally: list[TallyPart]


class CredentialRecord(CredentialRow):
    """What one service's credential reads, each reading or listing it sees."""

    manages: list[str]
    sights: list[dict[str, Any]]


class Tailnet(TypedDict):
    """The tailnet's policy and what HQ makes of it."""

    known: bool
    declaration: str | None
    counts: dict[str, int]
    settings: list[dict[str, Any]]
    grants: list[dict[str, Any]]
    ssh_rules: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    services: list[dict[str, Any]]
    app_connectors: list[dict[str, Any]]
    tests: list[dict[str, Any]]
    locked_out: list[str]
    findings: list[dict[str, Any]]
    unread: list[dict[str, Any]]
    credential_refusal: str | None


class ConnectionStandingRow(TypedDict):
    id: str
    connection_ref: str | None
    label: str
    kind: str
    family: str
    status: str
    status_label: str
    detail: str
    observed_at: str | None
    open_problems: int


class ConnectionStandingRecord(ConnectionStandingRow):
    """One connection whole: what its credential sees and changes, how it is reached, and the fix."""

    controller_id: str | None
    lifecycle: str
    endpoint: str | None
    authority: str
    credential_model: str
    scopes_known: bool
    granted_scopes: list[str]
    facts: list[dict[str, Any]]
    actions: list[WrittenAction]
    dependencies: list[dict[str, Any]]
    targets: list[dict[str, Any]]
    reads: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    family_label: str
    used_for: str | None
    secret_store: str | None
    reach: dict[str, Any] | None
    sight: dict[str, Any] | None
    would_also_see: list[str]
    credential_fix: dict[str, Any] | None
    last_activity: dict[str, Any] | None
    read_now: WrittenAction | None
    read_requested_at: str | None


class Match(TypedDict):
    """One search match: a machine, service or domain, or a record."""

    kind: str
    name: str
    label: str
    summary: str
    url: str
    group: str


# ----- Documentation ---------------------------------------------------------


class DocumentationRow(TypedDict):
    """One documentation pointer; a path or note it may not show is blank."""

    doc_id: str
    title: str
    doc_type: str
    system: str
    environment: str
    status: str
    sensitivity: str
    obsidian_path: str
    github_path: str
    external_url: str
    last_reviewed: str | None
    published_at: str | None
    notes: str
    updated_at: str


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
