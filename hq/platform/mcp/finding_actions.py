"""The open problems as ``get_findings`` answers them: each action the tool call that carries it out.

An agent cannot follow a web path, so a finding's actions here are calls, built
from what the finding already holds and never read back out of a URL: a remedy
or a command step runs through ``execute_capability``, an investigation is the
trace ``action_links.INVESTIGATIONS`` declares, an offer reads its subject
through the resource a topology node is read by, and "Check again" is this
read narrowed to the finding's rule. An action none of these derives is left
out. The machine API keeps the shared shape (``findings.serialize_finding``).
"""

from dataclasses import asdict
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast

from pydantic import ConfigDict, with_config

from hq.platform.application.action_links import INVESTIGATIONS, READ_NOW_CAPABILITY
from hq.platform.application.answer_fields import Pinned, SchemaVersion
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.describe import Describe
from hq.platform.application.finding_model import Finding, OperatorStep
from hq.platform.application.findings import (
    NODE_READ,
    FindingEvidence,
    FindingsSummary,
    SerializedFinding,
    narrowed_findings,
    serialize_finding,
)
from hq.platform.application.json_schemas import Written
from hq.platform.application.security import Principal
from hq.platform.application.workflow_contracts import ActionLink, WorkflowOutcome

# The shape get_findings answers in, apart from the API's: it moves when this shape does.
AGENT_FINDINGS_SCHEMA_VERSION = 3

_CLOSED = ConfigDict(extra="forbid")

# Each finding carries its evidence and every call that acts on it, so an
# answer holds the most severe few unless asked for more, whatever the estate
# and its extensions raise. The API answers every one: its query is the same,
# so absent is the door's to read.
FIRST_FINDINGS = 2
type Label = Annotated[str, Describe("What the call does here, in words a person reads.")]


@with_config(_CLOSED)
class CapabilityArguments(TypedDict):
    name: Annotated[str, Describe("The capability to run.")]
    target: Annotated[str | None, Describe("The record it runs on; null for a capability that takes none.")]
    payload: Annotated[
        dict[str, Any],
        Describe(
            "What HQ already knows the command takes. Add an `idempotency_key` where its effect is not "
            "`read`, and any field `describe_capabilities` names as required."
        ),
    ]


@with_config(_CLOSED)
class CapabilityCall(TypedDict):
    """A command, run through ``execute_capability``."""

    label: Label
    tool: Literal["execute_capability"]
    arguments: CapabilityArguments


@with_config(_CLOSED)
class TopologyArguments(TypedDict):
    focus: Annotated[str, Describe("The finding's subject, the node the trace starts from.")]
    direction: NotRequired[Annotated[str, Describe("Which way the trace follows the edges.")]]
    depth: NotRequired[Annotated[int, Describe("How many hops the trace follows.")]]


@with_config(_CLOSED)
class TopologyCall(TypedDict):
    """A trace of the topology around the finding's subject, read through ``get_topology``."""

    label: Label
    tool: Literal["get_topology"]
    arguments: TopologyArguments


@with_config(_CLOSED)
class RecordArguments(TypedDict):
    name: Annotated[str, Describe("The kind of record to read.")]
    identifier: Annotated[str, Describe("The record's identifier, the finding's subject.")]


@with_config(_CLOSED)
class RecordCall(TypedDict):
    """One record, read through ``get_resource``."""

    label: Label
    tool: Literal["get_resource"]
    arguments: RecordArguments


@with_config(_CLOSED)
class FindingsArguments(TypedDict):
    rule: Annotated[str, Describe("The finding's rule, so the read runs its check alone.")]


@with_config(_CLOSED)
class FindingsCall(TypedDict):
    """This read again, narrowed to one rule, through ``get_findings``."""

    label: Label
    tool: Literal["get_findings"]
    arguments: FindingsArguments


type Call = CapabilityCall | TopologyCall | RecordCall | FindingsCall


@with_config(_CLOSED)
class RemedyCall(CapabilityCall):
    """A command that would fix it."""

    effect: Annotated[
        str,
        Describe(
            "What running it does, in its command's own terms: `read`, `remote_write` or "
            "`infrastructure_change`. A `destructive` one is never suggested."
        ),
    ]


@with_config(_CLOSED)
class AgentWorkflowStep(TypedDict):
    phase: Annotated[
        str,
        Describe(
            "What the step is: `act` runs a remedy, `verify` checks it worked, `run` is an exact command the "
            "operator runs, `do` an instruction in words, `cannot` why it cannot be done from HQ."
        ),
    ]
    label: Annotated[str, Describe("The step's heading; empty for an instruction its summary says alone.")]
    summary: Annotated[str, Describe("The step in full; empty when its label says it.")]
    state: Annotated[
        str,
        Describe(
            "Where it stands: `recommended` to do now, `after_action` once the step before is done, "
            "`available` at any time, `operator` for a person to do by hand, `blocked` where it cannot be done."
        ),
    ]
    calls: Annotated[list[Call], Describe("The calls that carry it out, in order.")]


@with_config(_CLOSED)
class AgentWorkflow(TypedDict):
    id: Annotated[str, Describe("The plan's id, the same at every read of the problem it resolves.")]
    label: Annotated[str, Describe("The plan's heading.")]
    steps: Annotated[list[AgentWorkflowStep], Describe("Its steps, in the order they are taken.")]
    outcome: Annotated[dict[str, Any], Written(WorkflowOutcome)]


@with_config(_CLOSED)
class AgentFinding(TypedDict):
    """One finding, its every action a tool call."""

    id: Annotated[str, Describe("The problem's id, the same at every read while it is open.")]
    rule: Annotated[str, Describe("The rule that found it, as the `rule` argument takes it.")]
    subject: Annotated[str | None, Describe("The id of the topology node it is about; null when it has none.")]
    scope: Annotated[str | None, Describe("The record type, reading type or check it is about as a whole.")]
    affected_scopes: Annotated[list[str], Describe("Other record types or reading types it bears on.")]
    title: Annotated[str, Describe("The problem, in a line.")]
    severity: Annotated[str, Describe("How serious it is.")]
    explanation: Annotated[str, Describe("Why it matters and what it rests on, in sentences.")]
    evidence: Annotated[list[FindingEvidence], Describe("What it rests on, a labeled line each.")]
    remedies: Annotated[
        list[RemedyCall], Describe("The commands that would fix it which this caller may run, none destructive.")
    ]
    offers: Annotated[list[RecordCall], Describe("Reads of its subject to make before acting.")]
    investigations: Annotated[
        list[TopologyCall], Describe("Traces around its subject: its neighborhood and what depends on it.")
    ]
    workflow: Annotated[
        AgentWorkflow | None,
        Describe("The plan that resolves it, a fix and then the check that confirms it; null when it has neither."),
    ]
    operator_steps: Annotated[
        list[Annotated[dict[str, Any], Written(OperatorStep)]],
        Describe("What a person does by hand to resolve it, in order. HQ never runs these."),
    ]
    no_help_reason: Annotated[str | None, Describe("Why HQ can neither run a fix nor name an exact command for it.")]
    since: Annotated[str | None, Describe("When it was first seen open, an ISO 8601 instant; null where unrecorded.")]


@with_config(_CLOSED)
class AgentFindings(TypedDict):
    """What ``get_findings`` answers."""

    schema_version: Annotated[SchemaVersion, Pinned(AGENT_FINDINGS_SCHEMA_VERSION)]
    rule: Annotated[str | None, Describe("The rule the read was narrowed to; null for every rule's.")]
    summary: FindingsSummary
    findings: Annotated[list[AgentFinding], Describe("The problems, most severe first.")]


def _command(label: str, capability: str, target: str, payload: dict[str, Any] | None = None) -> CapabilityCall:
    # A target where the capability takes one, or execute_capability refuses the call.
    takes = bool(target) and bool(capability_registry()[capability].target_kind)
    arguments: CapabilityArguments = {"name": capability, "target": target if takes else None, "payload": payload or {}}
    return {"label": label, "tool": "execute_capability", "arguments": arguments}


def _trace(link: ActionLink, finding: Finding) -> TopologyCall | None:
    investigation = INVESTIGATIONS.get(link.name)
    if investigation is None or not finding.subject:
        return None
    arguments: TopologyArguments = {"focus": finding.subject}
    if investigation.direction:
        arguments["direction"] = investigation.direction
    if investigation.depth is not None:
        arguments["depth"] = investigation.depth
    return {"label": link.label, "tool": "get_topology", "arguments": arguments}


def _read(link: ActionLink, finding: Finding) -> RecordCall | None:
    # A read of the subject's own page; a page elsewhere (its connection's
    # settings, outside documentation) has no record to read.
    if link.name != "open" or not finding.subject:
        return None
    return {
        "label": link.label,
        "tool": "get_resource",
        "arguments": {"name": NODE_READ, "identifier": finding.subject},
    }


def _calls(link: ActionLink, finding: Finding) -> list[Call | None]:
    if link.name != "verify":
        command = _command(link.label, link.capability, link.target) if link.capability else None
        return [command or _trace(link, finding) or _read(link, finding)]
    # A check reads again what it rests on, where it asks to, then runs the check.
    read = finding.read_now if link.capability == READ_NOW_CAPABILITY else None
    again: FindingsCall = {"label": link.label, "tool": "get_findings", "arguments": {"rule": finding.rule}}
    if read is None:
        return [again]
    payload = {field: value for field, value in asdict(read).items() if value}
    return [_command(link.label, link.capability, "", payload), again]


def _unique[T: Call](calls: list[T | None]) -> list[T]:
    kept: list[T] = []
    for call in calls:
        if call is not None and all(
            call["tool"] != seen["tool"] or call["arguments"] != seen["arguments"] for seen in kept
        ):
            kept.append(call)
    return kept


def agent_finding(finding: Finding) -> AgentFinding:
    shared: SerializedFinding = serialize_finding(finding)
    workflow = finding.workflow
    return {
        "id": shared["id"],
        "rule": shared["rule"],
        "subject": shared["subject"],
        "scope": shared["scope"],
        "affected_scopes": shared["affected_scopes"],
        "title": shared["title"],
        "severity": shared["severity"],
        "explanation": shared["explanation"],
        "evidence": shared["evidence"],
        "remedies": [
            {**_command(remedy.label, remedy.capability, remedy.target), "effect": remedy.effect}
            for remedy in finding.remedies
        ],
        "offers": _unique([_read(link, finding) for link in finding.offers]),
        "investigations": _unique([_trace(link, finding) for link in finding.investigations]),
        "workflow": (
            {
                "id": workflow.id,
                "label": workflow.label,
                "steps": [
                    {
                        "phase": step.phase,
                        "label": step.label,
                        "summary": step.summary,
                        "state": step.state,
                        "calls": _unique([call for link in step.actions for call in _calls(link, finding)]),
                    }
                    for step in workflow.steps
                ],
                "outcome": asdict(workflow.outcome),
            }
            if workflow is not None
            else None
        ),
        "operator_steps": shared["operator_steps"],
        "no_help_reason": shared["no_help_reason"],
        "since": shared["since"],
    }


def agent_findings(*, principal: Principal, **narrowing: object) -> AgentFindings:
    """The open problems as ``get_findings`` answers them, the most severe
    ``FIRST_FINDINGS`` without a ``limit``; ``narrowing`` is ``FindingsQuery``'s."""

    if narrowing.get("limit") is None:
        narrowing["limit"] = FIRST_FINDINGS
    read = narrowed_findings(principal=principal, **narrowing)
    return cast(AgentFindings, read.answer(AGENT_FINDINGS_SCHEMA_VERSION, agent_finding))
