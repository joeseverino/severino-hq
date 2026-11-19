"""The shapes the infrastructure resource answers in: the records HQ manages,
their operations, and what HQ derives from each."""

from typing import Annotated, Any, NotRequired, TypedDict

from hq.domains.control_plane.models import OperationRequest

from .answer_fields import AsRead, DateTime, ObservedAt, UpdatedAt
from .conditions import DEGRADED, DRIFTED, READY
from .describe import Describe, listed
from .infrastructure import RecordState
from .ui import ended


class ConditionPart(TypedDict):
    type: Annotated[
        str,
        Describe(ended(f"What the condition says of the record, such as {listed((READY, DRIFTED, DEGRADED))}")),
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
type AskedAt = Annotated[str, DateTime(), Describe("When it was asked for, an ISO 8601 date-time.")]
type CompletedAt = Annotated[
    Annotated[str, DateTime()] | None, Describe("When it finished, an ISO 8601 date-time; null until it has.")
]
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
    by: Annotated[str, Describe("Who asked for it, through which door, and who approved it, as HQ's pages name them.")]
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


class ActionPart(TypedDict):
    enabled: Annotated[bool, Describe("Whether it may be asked for now.")]
    reason: Annotated[str, Describe("Why it may not, as a sentence; empty when it may.")]
    automatic: Annotated[bool, Describe("Whether the controller does it on its own, unasked.")]


class RemovalPart(TypedDict):
    """Whether and how it may be removed."""

    mode: Annotated[
        str,
        Describe("How it is removed: `delete` at its provider, `forget` HQ's record of it, or `unavailable`."),
    ]
    reason: Annotated[str, Describe("Why it may not be removed now; empty when it may.")]
    pending: Annotated[bool, Describe("Whether a removal is in flight.")]


class StandingPart(TypedDict):
    """Where it stands, as the one line its page and its row say it."""

    state: Annotated[str, Describe(ended(f"Its state in one word: {listed(tuple(RecordState))}"))]
    tone: Annotated[
        str,
        Describe(
            ended(f"How its state is colored: {listed(tuple(dict.fromkeys(state.tone for state in RecordState)))}")
        ),
    ]
    label: Annotated[str, Describe("Its state in the words a page shows, such as `Change waiting to apply`.")]
    detail: Annotated[str, Describe("What the state means for it, as a sentence; empty for none.")]
    since: Annotated[
        Annotated[str, DateTime()] | None,
        Describe("When it came to stand so, an ISO 8601 date-time; null when unsaid."),
    ]
    read_at: Annotated[
        Annotated[str, DateTime()] | None, Describe("When it was last read, an ISO 8601 date-time; null when never.")
    ]


class MachinePointer(TypedDict):
    name: Annotated[str, Describe("Which machine, by its name.")]
    url: Annotated[str, Describe("That machine's page in HQ.")]


class ServicePointer(TypedDict):
    hostname: Annotated[str, Describe("The service's hostname.")]
    url: Annotated[str, Describe("That service's page in HQ.")]


class ReadoutPart(TypedDict):
    label: Annotated[str, Describe("The setting, as its type's card labels it.")]
    desired: Annotated[str, Describe("What HQ expects of it; empty when HQ expects nothing.")]
    observed: Annotated[str, Describe("What was last read of it; empty when not read.")]


class ExpiryPart(TypedDict):
    not_after: Annotated[str, DateTime(), Describe("When the certificate expires, an ISO 8601 date-time.")]
    days_left: Annotated[int, Describe("Whole days until it expires.")]
    renewal_at: Annotated[str, DateTime(), Describe("When its renewal window opens, an ISO 8601 date-time.")]


class CertificatePart(TypedDict):
    """A certificate as the controller resolves its declaration."""

    certificate_name: Annotated[str, Describe("The name the certificate is filed under.")]
    renewal_window_days: Annotated[int, Describe("How many days before it expires it renews.")]
    domains: Annotated[list[str], Describe("The names it is issued for.")]


class ConsumerPart(TypedDict):
    name: Annotated[str, Describe("Where the certificate is installed, by the name its declaration gives it.")]
    kind: Annotated[str, Describe("What kind of place it is.")]
    domains: Annotated[list[str], Describe("The names checked there: those last read, else those it is told to check.")]
    connection_ref: Annotated[str, Describe("The connection that installs it there, by ref; empty for none.")]
    url: Annotated[str, Describe("The page in HQ of the record that delivers it there; empty for none.")]


class ManagedResourceRecord(ManagedResourceItem):
    """One managed record whole, what HQ derives from it, and the operations run on it."""

    url: Annotated[str, Describe("Its page in HQ: its own, or the page of the machine it declares.")]
    name: Annotated[str, Describe("The name its page is titled by.")]
    standing: StandingPart
    actions: Annotated[
        dict[str, ActionPart], Describe("Each action of its controller, by verb, and whether it may run.")
    ]
    removal: RemovalPart
    origin_machine: Annotated[MachinePointer | None, Describe("The machine it sends traffic to; null for none.")]
    provider_machine: Annotated[
        MachinePointer | None, Describe("The machine the provider that manages it runs on; null when unknown.")
    ]
    services: Annotated[list[ServicePointer], Describe("The services it is part of.")]
    readout: Annotated[list[ReadoutPart], Describe("Its settings, what HQ expects beside what was last read.")]
    expiry: Annotated[ExpiryPart | None, Describe("A certificate's expiry and renewal; null for anything else.")]
    awaiting_approval: Annotated[int, Describe("How many changes to it wait for an operator's approval.")]
    certificate: Annotated[
        CertificatePart | None, Describe("A certificate as the controller resolves it; null for anything else.")
    ]
    consumers: Annotated[list[ConsumerPart], Describe("Where a certificate is installed.")]
    resolution_error: Annotated[str, Describe("Why a certificate could not be resolved; empty when it was.")]
    used_by: Annotated[list[str], Describe("The hostnames served with a certificate.")]
    unused_names: Annotated[list[str], Describe("The names on a certificate nothing is served under.")]
    operations: Annotated[list[OperationSummary], Describe("The latest operations that changed it, newest first.")]
