"""Claims about the estate, with the evidence behind them and what to run.

A finding is an answer arriving without being asked. A resource can be wrong
while every surface reads healthy, so the rules are about the shapes a silence
can take, each derivable from the projection alone, which is why an extension
gets them without the host learning what it is. Deriving is pure, and a remedy
is a registered capability, absent for whoever cannot run it (``_resolved``).
"""

import inspect
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast
from urllib.parse import urlencode

from django.conf import settings
from pydantic import ConfigDict, Field, create_model, with_config

from hq.domains.control_plane.providers import PROVIDERS, enabled_controller_actions
lazy from hq.domains.control_plane.models import OperationRequest
lazy from hq.domains.control_plane.names import normalized_hostname
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND

from . import (
    automation_findings,
    certificate_expiry,
    connection_findings,
    container_findings,
    contradiction_findings,
    controller_findings,
    credential_findings,
    dns_findings,
    docker_estate,
    filesystem_findings,
    github_findings,
    perimeter_findings,
    posture_findings,
    reboot_findings,
    reference_findings,
    registration_findings,
    render_status_findings,
    tailnet_device_findings,
    tailnet_findings,
    unit_findings,
)
from .action_links import (
    ActionLink,
    action_with_return,
    command_action,
    read_now_link,
    topology_investigation_links,
)
from .answer_fields import Pinned, SchemaVersion
from .contracts import route_url
from .derivations import derivation, present
from .derived_inputs import ESTATE_READS, estate_variant
from .describe import Describe, listed
from .finding_model import (
    CHECK_AGAIN,
    Finding,
    FindingEstate,
    FindingRemedy,
    FindingRule,
    OperatorStep,
    is_observable,
    parse_stamp,
)
from .first_seen import recorded
from .integration_validation import required_capability_names
from .integrations import IntegrationGraph, integration_graph
from .json_schemas import Written
from .projection import QUERY
from .security import Capability, Principal
from .topology import derive_topology
from .topology_model import (
    JOINED_KINDS,
    STANDINGS,
    Topology,
    TopologyNode,
)
from .ui import ended
from .workflows import (
    WrittenAction,
    WrittenWorkflow,
    claim_identity,
    claim_resolution_plan,
    serialize_workflow,
)
lazy from . import cadence
lazy from .automation_runs import playbook_remedies
lazy from .estate import estate_reading
lazy from .moments import ago
lazy from .ui import counted

_CLAIM_NAMESPACE = "infrastructure.finding"


def _causal_edges(topology: Topology) -> dict[str, dict[str, set[str]]]:
    """Index only the relationship verbs causal findings traverse."""

    indexed = {kind: {} for kind in ("carries", "enables", "governs", "used_by")}
    for edge in topology.edges:
        if edge.kind == "governs":
            indexed[edge.kind].setdefault(edge.source, set()).add(edge.target)
        elif edge.kind in indexed:
            indexed[edge.kind].setdefault(edge.target, set()).add(edge.source)
    return indexed


def _controllers_by_kind(topology: Topology, by_id: dict[str, TopologyNode]) -> dict[str, frozenset[str]]:
    """Controllers a kind can be attributed to without guessing."""

    controllers_by_kind: dict[str, set[str]] = {}
    for resource_id, controllers in _proven_controllers(_causal_edges(topology)):
        node = by_id.get(resource_id)
        if node is not None and node.kind_key and controllers:
            controllers_by_kind.setdefault(node.kind_key, set()).update(controllers)
    return {kind: frozenset(items) for kind, items in controllers_by_kind.items()}


def _proven_controllers(indexed: dict[str, dict[str, set[str]]]) -> Iterator[tuple[str, set[str]]]:
    """Each resource the edges reach, with the controllers proven to read it:
    those carrying a connection it is used by, and the one enabling an ability
    that governs it."""

    for resource_id, connections in indexed["used_by"].items():
        yield resource_id, _carrying(indexed, connections)
    # Ability nodes are shared by a connection family. They prove a cause only
    # when exactly one controller enables that ability; otherwise attributing
    # every governed resource to both would manufacture knowledge HQ lacks.
    for ability, connections in indexed["enables"].items():
        controllers = _carrying(indexed, connections)
        if len(controllers) == 1:
            yield from ((resource_id, controllers) for resource_id in indexed["governs"].get(ability, set()))


def _carrying(indexed: dict[str, dict[str, set[str]]], connections: set[str]) -> set[str]:
    """The controllers that carry any of ``connections``."""

    return {controller for connection in connections for controller in indexed["carries"].get(connection, set())}


def _estate(topology: Topology) -> FindingEstate:
    observed: dict[str, datetime] = {}
    for node in topology.nodes:
        moment = parse_stamp(node.observed_at)
        if moment is None or not node.kind_key or node.kind in JOINED_KINDS:
            continue
        observed[node.id] = moment
    # A kind's read, never its newest record: HQ writes a record's observation
    # when it applies or adopts it, which says nothing of whether the kind
    # was read.
    latest = dict(topology.kinds_read)
    governed = frozenset(edge.target for edge in topology.edges if edge.kind == "governs")
    counts = Counter(
        node.kind_key for node in topology.nodes if node.kind == "resource" and node.managed and node.kind_key
    )
    by_id = {node.id: node for node in topology.nodes}
    return FindingEstate(
        topology,
        present(),
        latest,
        observed,
        governed,
        frozenset(counts),
        counts,
        _controllers_by_kind(topology, by_id),
    )


# Every module that raises findings declares its rules beside its detectors.
# This is the closed set, in the order pages list the rules.
RULE_MODULES = (
    docker_estate,
    container_findings,
    perimeter_findings,
    connection_findings,
    credential_findings,
    github_findings,
    render_status_findings,
    unit_findings,
    filesystem_findings,
    reboot_findings,
    certificate_expiry,
    registration_findings,
    reference_findings,
    controller_findings,
    tailnet_findings,
    tailnet_device_findings,
    dns_findings,
    contradiction_findings,
    posture_findings,
    automation_findings,
)
RULES: tuple[FindingRule, ...] = tuple(rule for module in RULE_MODULES for rule in module.RULES)

_RULE_BY_NAME = {rule.name: rule for rule in RULES}
if len(_RULE_BY_NAME) != len(RULES):
    raise ValueError("Two finding modules use the same rule name.")


def finding_steps(finding: Finding) -> tuple[OperatorStep, ...]:
    """What a person does to resolve it: its own steps, else its rule's."""

    if finding.steps:
        return finding.steps
    found = _RULE_BY_NAME.get(finding.rule)
    return (OperatorStep(label=found.operator_action),) if found else ()


def finding_rules() -> tuple[FindingRule, ...]:
    """Every claim HQ knows how to make about itself."""

    return RULES


@dataclass(frozen=True, slots=True)
class Coverage:
    """What an empty findings page was checked over, so "no problems" says how far it goes."""

    rules: int
    machines: int
    services: int
    # When the controller last reported; None when it never has.
    read_at: datetime | None

    @property
    def sentence(self) -> str:
        return (
            f"{counted(self.rules, 'check')} over {counted(self.machines, 'machine')} and "
            f"{counted(self.services, 'service')}, from readings {ago(self.read_at)}."
        )


def coverage() -> Coverage:
    estate = estate_reading()
    return Coverage(len(RULES), len(estate.machines), len(estate.services), cadence.controller_standing().seen_at)


def rule_for(name: str) -> FindingRule | None:
    return _RULE_BY_NAME.get(name)


@cache
def rule_origin(name: str) -> dict[str, Any] | None:
    """Where a rule comes from, as a card's panel says it: its name, title and
    severity, what it stands in for, and the file and line its detector is
    declared at, relative to the checkout. Rules are fixed at import, so this
    is read once per process."""

    rule = rule_for(name)
    if rule is None:
        return None
    where = ""
    try:
        source, line = _declared_at(rule.detect)
        where = f"{source.relative_to(settings.BASE_DIR)}:{line}"
    except OSError, StopIteration, TypeError, ValueError:
        where = ""
    return {
        "rule": rule.name,
        "title": rule.title,
        "severity": rule.severity,
        "subsumes": list(rule.subsumes),
        "where": where,
    }


def _declared_at(detect: Callable[..., Any]) -> tuple[Path, int]:
    """A detector's file and line: a function's own, or for a declaration such
    as an ``OnEachMachine``, which ``inspect`` cannot place, the line binding
    it in the rule module that holds it."""

    if inspect.isfunction(detect):
        return Path(inspect.getsourcefile(detect) or ""), inspect.getsourcelines(detect)[1]
    module = next(module for module in RULE_MODULES if any(rule.detect is detect for rule in module.RULES))
    name = next(name for name, value in vars(module).items() if value is detect)
    lines, _start = inspect.getsourcelines(module)
    return Path(inspect.getsourcefile(module) or ""), next(
        number for number, text in enumerate(lines, 1) if text.startswith(f"{name} =")
    )


def _permitted(capability: str, principal: Principal, graph: IntegrationGraph) -> tuple[bool, str]:
    """Whether this principal may run it, and what the registry says it does."""

    spec = graph.capabilities.get(capability)
    if spec is not None:
        return principal.permits(*spec.required_capabilities), spec.effect
    # A rule naming a capability the registry does not hold is a contract error
    # the suite catches; at runtime the honest answer is to offer nothing.
    return False, ""


def _playbooks(finding: Finding, subject: TopologyNode | None) -> tuple[FindingRemedy, ...]:
    """The adopted playbooks that say they fix a finding about a machine, each with that machine chosen."""

    if subject is None or subject.kind != MACHINE_KIND:
        return ()
    return playbook_remedies(finding.rule, normalized_hostname(subject.label))


def _resolved(finding: Finding, principal: Principal, subject: TopologyNode | None) -> Finding:
    """Drop remedies this principal cannot run, and take effect from the spec.

    Absent rather than disabled: an offer that cannot work is worse than no
    offer. The effect is copied from the capability registry rather than
    restated by the rule, so a capability that becomes destructive tomorrow is
    described as destructive tomorrow without anyone editing a rule.
    """

    graph = integration_graph()
    kept = []
    for remedy in (*finding.remedies, *_playbooks(finding, subject)):
        allowed, effect = _permitted(remedy.capability, principal, graph)
        if not allowed or effect == "destructive":
            continue
        action = next(
            (
                candidate
                for candidate in (subject.actions if subject else ())
                if candidate.capability == remedy.capability and candidate.target == remedy.target
            ),
            None,
        )
        kept.append(
            FindingRemedy(
                capability=remedy.capability,
                target=remedy.target,
                intent=remedy.intent,
                variant=remedy.variant,
                effect=effect,
                url=(action_with_return(action, "control_plane:findings").url if action else remedy.url),
                method=action.method if action else remedy.method,
                auto=False,
            )
        )
    resolved_remedies = tuple(kept)
    offers = finding.offers + tuple(
        action for action in (subject.actions if subject else ()) if action.effect == "read" and action.method == "GET"
    )
    investigations = topology_investigation_links(subject.id) if subject else ()
    remedy_actions = tuple(
        command_action(
            "remedy",
            remedy.capability,
            remedy.url,
            method=remedy.method,
            target=remedy.target,
            variant=remedy.variant,
            intent=remedy.intent,
            recommended=True,
        )
        for remedy in resolved_remedies
        if remedy.url
    )
    verification, read_now = _verification(finding, principal, subject)
    workflow = claim_resolution_plan(
        namespace=_CLAIM_NAMESPACE,
        rule=finding.rule,
        subject=finding.subject,
        scope=finding.scope,
        remedies=remedy_actions,
        verification=verification,
    )
    # Every field the detector set carries through; only what derivation adds changes.
    return replace(
        finding,
        remedies=resolved_remedies,
        offers=offers,
        investigations=investigations,
        workflow=workflow,
        steps=finding_steps(finding),
        no_help_reason=_no_help_reason(finding),
        subject_link=ActionLink("subject", subject.label, "read", subject.url) if subject and subject.url else None,
        read_capabilities=_held_to(finding, subject, principal),
        read_now=read_now,
    )


# The resource each part of a finding is read through, whose declared
# capabilities are that part's read gate: a topology node through the
# relationships around it, a declared kind through its records, an observed
# kind through its readings, a check of HQ's standard through the connection
# standing that reports it.
NODE_READ = "relationships"
_RECORDS_READ = "infrastructure"
_READINGS_READ = "readings"
_POSTURE_READ = "connections"


def read_gate(finding: Finding, subject: TopologyNode | None) -> tuple[str, ...] | None:
    """What a reader must hold to be told ``finding``; None when it cannot be derived.

    The union of its subject node's gate, each scope's and what its detector
    carried for the evidence it names, which alone gates a claim about none
    of them. A subject that names no node, a scope no resource reads, or a
    claim about nothing that carries no gate leaves it underivable.
    """

    scopes = _finding_scopes(finding)
    if (finding.subject and subject is None) or not (subject or scopes or finding.read_capabilities):
        return None
    gates = [finding.read_capabilities]
    if subject is not None:
        gates += [_resource_gate(NODE_READ), subject.read_capabilities]
    for scope in scopes:
        if (resource := _scope_resource(scope)) is None:
            return None
        gates.append(_resource_gate(resource))
    return tuple(dict.fromkeys(name for gate in gates for name in gate))


def _held_to(finding: Finding, subject: TopologyNode | None, principal: Principal) -> tuple[str, ...]:
    # Underivable fails closed: held at the authority it was derived at.
    gate = read_gate(finding, subject)
    return gate if gate is not None else tuple(sorted(str(item) for item in principal.capabilities))


def _scope_resource(scope: str) -> str | None:
    if posture_findings.posture_check(scope) is not None:
        return _POSTURE_READ
    if scope in OBSERVATIONS:
        return _READINGS_READ
    if scope in PROVIDERS:
        return _RECORDS_READ
    return None


def _resource_gate(name: str) -> tuple[str, ...]:
    return required_capability_names(integration_graph().resources[name])


def _no_help_reason(finding: Finding) -> str:
    """The finding's own reason, else its rule's, when it has no remedy to
    offer and no exact command to name. Judged before a principal's remedies
    are filtered: a remedy this reader may not run is still a remedy."""

    if finding.remedies or any(step.command for step in finding.steps):
        return ""
    found = _RULE_BY_NAME.get(finding.rule)
    return finding.no_help_reason or (found.no_help_reason if found else "")


def _read_subject(finding: Finding, subject: TopologyNode | None) -> cadence.ControllerSweepCommand | None:
    """What to read now so the check re-runs on fresh data; None when no reading
    is involved."""

    if subject is not None and subject.kind == "connection" and subject.connection_ref:
        return cadence.ControllerSweepCommand(connection_ref=subject.connection_ref)
    kind = finding.scope or (subject.kind_key if subject is not None else "")
    if kind in OBSERVATIONS or (kind in PROVIDERS and is_observable(kind)):
        return cadence.ControllerSweepCommand(kind=kind)
    if finding.affected_scopes:
        return cadence.ControllerSweepCommand(every_connection=True)
    return None


def _verification(
    finding: Finding, principal: Principal, subject: TopologyNode | None
) -> tuple[ActionLink | None, cadence.ControllerSweepCommand | None]:
    """Refresh and check again where a reading is involved; else check again.
    With the link, the read it asks for, when it asks for one."""

    findings_url = route_url("control_plane:findings")
    if not findings_url:
        return None, None
    wanted = _read_subject(finding, subject)
    read = read_now_link(principal, **asdict(wanted)) if wanted is not None else None
    again = f"{findings_url}?{urlencode({'rule': finding.rule})}"
    if read is not None:
        separator = "&" if "?" in read.url else "?"
        verify = command_action(
            "verify",
            read.capability,
            f"{read.url}{separator}{urlencode({'next': again})}",
            method="POST",
            target=read.target,
            intent=CHECK_AGAIN,
            reason="Reads it now, then checks again.",
        )
        return verify, wanted
    verify = ActionLink(
        "verify",
        CHECK_AGAIN,
        "read",
        again,
        reason="Runs this check against current data.",
    )
    return verify, None


def _finding_scopes(finding: Finding) -> tuple[str, ...]:
    return ((finding.scope,) if finding.scope else ()) + finding.affected_scopes


def _silenced_kinds(
    raised: dict[str, tuple[Finding, ...]],
) -> set[str]:
    return {
        scope
        for declared in RULES
        for finding in raised[declared.name]
        if declared.subsumes
        for scope in _finding_scopes(finding)
    }


def _silenced_scopes(
    raised: dict[str, tuple[Finding, ...]],
) -> dict[str, set[str]]:
    silenced: dict[str, set[str]] = {}
    for declared in RULES:
        scopes = {scope for finding in raised[declared.name] for scope in _finding_scopes(finding)}
        for name in declared.subsumes:
            silenced.setdefault(name, set()).update(scopes)
    return silenced


def _subsumed_subjects(
    raised: dict[str, tuple[Finding, ...]],
) -> dict[str, set[str]]:
    subjects: dict[str, set[str]] = {}
    for declared in RULES:
        found = {finding.subject for finding in raised[declared.name] if finding.subject}
        for name in declared.subsumes:
            subjects.setdefault(name, set()).update(found)
    return subjects


def _is_suppressed(
    finding: Finding,
    declared: FindingRule,
    node: TopologyNode | None,
    *,
    exact_rule: bool,
    kinds: set[str],
    scopes: dict[str, set[str]],
    subjects: dict[str, set[str]],
) -> bool:
    """Whether a higher-order claim already says this fact more usefully."""

    if exact_rule:
        return False
    return (
        finding.subject in subjects.get(declared.name, set())
        or finding.scope in scopes.get(declared.name, set())
        or (node is not None and node.kind_key in kinds)
    )


def derive_findings(topology: Topology, *, principal: Principal, rule: str = "") -> tuple[Finding, ...]:
    """Every claim the projection supports, most serious first.

    Pure: no query, no write. The projection was already narrowed to what this
    principal may see, so a finding cannot reveal a node the caller could not
    already read.
    """

    estate = _estate(topology)
    wanted = _RULE_BY_NAME.get(rule) if rule else None
    raised: dict[str, tuple[Finding, ...]] = {}
    for declared in RULES:
        raised[declared.name] = declared.detect(estate)

    # A rule that fired takes its subsumed rules off the same subject, and off
    # the whole kind when it speaks for one.
    silenced_kinds = _silenced_kinds(raised)
    silenced_scopes_by_rule = _silenced_scopes(raised)
    subsumed_by = _subsumed_subjects(raised)

    by_id = {node.id: node for node in topology.nodes}
    findings = []
    for declared in RULES:
        if wanted is not None and declared.name != wanted.name:
            continue
        for finding in raised[declared.name]:
            node = by_id.get(finding.subject)
            if _is_suppressed(
                finding,
                declared,
                node,
                exact_rule=wanted is not None,
                kinds=silenced_kinds,
                scopes=silenced_scopes_by_rule,
                subjects=subsumed_by,
            ):
                continue
            findings.append(_resolved(finding, principal, node))

    return tuple(sorted(findings, key=lambda f: (_rank(f.severity), f.rule, f.subject)))


# Most severe first: the order findings are served in and a floor reads.
SEVERITIES = STANDINGS


def _rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


def estate_findings(*, principal: Principal, rule: str = "") -> tuple[Any, ...]:
    """Every finding the topology ``principal`` may see supports."""

    principal.require(Capability.READ)
    return _estate_findings(principal, rule)


@derivation(
    "estate.findings",
    reads=ESTATE_READS,
    vary=lambda principal, rule: (estate_variant(principal), rule),
)
def _estate_findings(principal: Principal, rule: str) -> tuple[Any, ...]:
    raised = derive_findings(derive_topology(principal=principal), principal=principal, rule=rule)
    seen = recorded() if raised else {}
    return tuple(replace(finding, since=seen.get(finding_key(finding))) for finding in raised)


def finding_key(finding: Finding) -> str:
    """What a finding is about, as its item in the queue is keyed."""

    about = f"finding:{finding.rule}:{finding.subject or finding.scope}"
    # Two containers on one machine are two matters, each keyed by its own.
    return f"{about}:{finding.part}" if finding.part else about


def rule_of(item_key: str) -> str:
    """The rule a queue item's key names when the item is a finding, read
    back from ``finding_key`` behind its source; empty for any other item."""

    _, found, rest = item_key.partition("finding:")
    return rest.split(":", 1)[0] if found else ""


class FindingEvidence(TypedDict):
    """One line of what a finding rests on."""

    label: Annotated[str, Describe("What this line of evidence is, as a problem card labels it.")]
    value: Annotated[Any, Describe("What was found, most often a name or a sentence.")]


# What running a suggested remedy does, as the API and MCP both say it.
type RemedyEffect = Annotated[
    str,
    Describe(
        "What running it does, in its command's own terms: `read`, `remote_write` or "
        "`infrastructure_change`. A `destructive` one is never suggested."
    ),
]


class SuggestedRemedy(TypedDict):
    """A capability that would answer a finding, and the target it runs on."""

    capability: Annotated[str, Describe("The command that would fix it, run with `target` as its target.")]
    target: Annotated[Any, Describe("The record the command runs on.")]
    label: Annotated[str, Describe("What running it does here, in the remedy's own words or its command's.")]
    effect: RemedyEffect
    auto: Annotated[
        bool,
        Describe("Whether HQ runs it unattended. A problem only proposes, so it reads false."),
    ]


class FindingFacts(TypedDict):
    """What a finding says the same at every door: what it is about, what it
    rests on, and what the operator does by hand. Each door adds its actions."""

    id: Annotated[
        str,
        Describe("The problem's id, the same at every read while it is open; its workflow's outcome names it."),
    ]
    rule: Annotated[str, Describe("The rule that found it, as the `rule` filter takes it.")]
    subject: Annotated[
        str | None,
        Describe("The id of the map node it is about. Null when it is about a whole type or check."),
    ]
    scope: Annotated[
        str | None,
        Describe("The record type, reading type or check it is about as a whole; null when it has none."),
    ]
    affected_scopes: Annotated[list[str], Describe("Other record types or reading types it bears on.")]
    title: Annotated[str, Describe("The problem, in a line.")]
    severity: Annotated[str, Describe(f"How serious it is: {listed(SEVERITIES)}, most severe first.")]
    explanation: Annotated[str, Describe("Why it matters and what it rests on, in sentences.")]
    evidence: Annotated[list[FindingEvidence], Describe("What it rests on, a labeled line each.")]
    operator_steps: Annotated[
        list[Annotated[dict[str, Any], Written(OperatorStep)]],
        Describe("What you do by hand to resolve it, in order. HQ never runs these."),
    ]
    no_help_reason: Annotated[
        str | None,
        Describe("Why HQ can neither run a fix nor name an exact command for it; null when it can do either."),
    ]
    since: Annotated[
        str | None,
        Describe("When it was first seen open, an ISO 8601 instant; null where no read has recorded it."),
    ]


class SerializedFinding(FindingFacts):
    """One finding as the API answers it; MCP's `get_findings` makes its actions tool calls."""

    remedies: Annotated[
        list[SuggestedRemedy],
        Describe("The commands that would fix it which this reader may run, none of them destructive."),
    ]
    offers: Annotated[list[WrittenAction], Describe("Reads of its subject this reader may run before acting.")]
    investigations: Annotated[
        list[WrittenAction],
        Describe("Where on the map to look around its subject: its neighborhood and what it affects."),
    ]
    workflow: Annotated[
        WrittenWorkflow | None,
        Describe("The plan that resolves it, a fix and then the check that confirms it; null when it has neither."),
    ]


def finding_facts(finding: Finding) -> FindingFacts:
    return {
        "id": claim_identity(_CLAIM_NAMESPACE, finding.rule, finding.subject, finding.scope),
        "rule": finding.rule,
        "subject": finding.subject or None,
        "scope": finding.scope or None,
        "affected_scopes": list(finding.affected_scopes),
        "title": finding.title,
        "severity": finding.severity,
        "explanation": finding.explanation,
        "evidence": [{"label": label, "value": value} for label, value in finding.evidence],
        "operator_steps": [asdict(step) for step in finding.steps],
        "no_help_reason": finding.no_help_reason or None,
        "since": finding.since.isoformat() if finding.since else None,
    }


def serialize_finding(finding: Finding) -> SerializedFinding:
    return {
        **finding_facts(finding),
        "remedies": [
            {
                "capability": remedy.capability,
                "target": remedy.target,
                "label": remedy.label,
                "effect": remedy.effect,
                "auto": remedy.auto,
            }
            for remedy in finding.remedies
        ],
        "offers": [asdict(action) for action in finding.offers],
        "investigations": [asdict(action) for action in finding.investigations],
        "workflow": serialize_workflow(finding.workflow),
    }


# Built from the declarations, so a type checker reads each as a value.
_RULE: Any = Literal[tuple(rule.name for rule in RULES)]
_SEVERITY: Any = Literal[SEVERITIES]

# The API's query and get_findings' arguments are both this model, so the two
# doors publish one schema and refuse one input in one sentence.
FindingsQuery = create_model(
    "FindingsQuery",
    __config__=QUERY,
    __doc__="What a read of the open problems is narrowed by.",
    rule=(_RULE | None, Field(None, description="Only this rule's problems; absent, every rule's.")),
    severity=(
        _SEVERITY | None,
        Field(None, description="The least severe finding to return: `attention` also returns `serious`."),
    ),
    limit=(
        int | None,
        Field(
            None,
            ge=1,
            description="How many problems to return, most severe first; the summary still counts every one.",
        ),
    ),
)


# The shape findings answers in; it moves when that shape does.
FINDINGS_SCHEMA_VERSION = 2


class FindingsSummary(TypedDict):
    """How many problems there are, and how many came back."""

    findings: Annotated[int, Describe("How many problems there are in all, counted before the floor and the limit.")]
    severities: Annotated[dict[str, int], Describe("How many there are at each severity, by severity.")]
    returned: Annotated[int, Describe("How many came back in the list.")]
    truncated: Annotated[bool, Describe("Whether the limit held back some that the floor let through.")]


# Closed, the findings within it too: every field is HQ's own, so one a
# serializer adds fails until it is declared here.
@with_config(ConfigDict(extra="forbid"))
class Findings(TypedDict):
    """What ``findings`` answers."""

    schema_version: Annotated[SchemaVersion, Pinned(FINDINGS_SCHEMA_VERSION)]
    rule: Annotated[str | None, Describe("The rule the read was narrowed to; null for every rule's.")]
    summary: FindingsSummary
    findings: Annotated[
        list[SerializedFinding],
        Describe("The problems, most severe first, as far as the floor and the limit let through."),
    ]


def findings(*, principal: Principal, **narrowing: object) -> Findings:
    """The serialized claims, in the shape the machine API answers; MCP writes its own (``mcp.finding_actions``)."""

    return cast(
        Findings, narrowed_findings(principal=principal, **narrowing).answer(FINDINGS_SCHEMA_VERSION, serialize_finding)
    )


@dataclass(frozen=True, slots=True)
class NarrowedFindings:
    """The findings a read returns, before a door writes them in its own shape."""

    rule: str | None
    summary: FindingsSummary
    kept: tuple[Finding, ...]

    def answer(self, version: int, write: Callable[[Finding], Any]) -> dict[str, Any]:
        """The read as a door answers it: its shape's version, and each finding as ``write`` writes it."""

        return {
            "schema_version": version,
            "rule": self.rule,
            "summary": self.summary,
            "findings": [write(finding) for finding in self.kept],
        }


def narrowed_findings(*, principal: Principal, **narrowing: object) -> NarrowedFindings:
    """The findings ``narrowing`` keeps and the summary of them, for every door
    that answers a read of the open problems.

    ``narrowing`` is judged by ``FindingsQuery`` once the caller may read.
    ``severity`` is a floor (``attention`` also returns ``serious``) and
    ``limit`` bounds how many are returned, most severe first. The summary
    still counts every finding, and says how many came back and whether the
    limit held some back.
    """

    principal.require(Capability.READ)
    query = FindingsQuery.model_validate(narrowing).model_dump()
    rule: str = query["rule"] or ""
    limit: int | None = query["limit"]
    floor = SEVERITIES.index(query["severity"]) if query["severity"] else len(SEVERITIES)
    raised = estate_findings(principal=principal, rule=rule)
    counts = Counter(finding.severity for finding in raised)
    eligible = [finding for finding in raised if _rank(finding.severity) <= floor]
    kept = tuple(eligible[:limit])
    return NarrowedFindings(
        rule=rule or None,
        summary={
            "findings": len(raised),
            "severities": counts,
            "returned": len(kept),
            "truncated": len(kept) < len(eligible),
        },
        kept=kept,
    )


# ----- what may be repaired without asking -------------------------------
#
# The graph is what makes this judgeable. A finding on its own says one record
# is wrong; the relationships around it say whether acting is sane. Three gates,
# all read off the projection rather than guessed:
#
#   - if the whole kind is unreached, the sweep is the fault and fanning a
#     reconcile across every record of it is an amplifier, not a repair;
#   - if the connection that governs the kind is not currently reachable, the
#     work would fail a minute later in a job result;
#   - and a cap, because a class-wide condition fires on the whole class at once.
#
# HQ forms no opinion about what is safe to run unattended. That judgment is
# already written down, per kind and per action, in the controller contract,
# so the only actions considered here are the ones it already declares
# automatic, and withdrawing one there withdraws it here.

_AUTO_RULES = ("skipped-by-a-sweep", "never-observed")


@dataclass(frozen=True, slots=True)
class Repair:
    """One finding judged safe to queue, and why."""

    resource_key: str
    rule: str
    reason: str


def auto_remediable(*, principal: Principal, limit: int = 10) -> tuple[Repair, ...]:
    """Findings whose remedy the controller contract already runs unattended.

    Returns what should be *queued*, never anything executed here. HQ queues and
    the controller pulls; reversing that would put a provider credential in the
    web process, which is the one property the cadence design exists to protect.
    """

    automatic_kinds = {
        kind
        for kind, action in enabled_controller_actions(automatic_only=True)
        if action == OperationRequest.Action.RECONCILE
    }
    if not automatic_kinds:
        return ()

    topology = derive_topology(principal=principal)
    raised = estate_findings(principal=principal)

    # A kind the sweep never reached: the fault is the sweep. Repairing each
    # record of it would queue the whole class against a provider that is not
    # answering, which is the amplifier this guard exists to prevent.
    unreached = {finding.scope for finding in raised if finding.rule == "kind-never-swept"}
    unreachable = _unreachable_kinds(topology)
    by_id = {node.id: node for node in topology.nodes}

    repairs = []
    for finding in raised:
        if finding.rule not in _AUTO_RULES or not finding.remedies:
            continue
        node = by_id.get(finding.subject)
        if node is None or node.kind_key not in automatic_kinds:
            continue
        if node.kind_key in unreached or node.kind_key in unreachable:
            continue
        repairs.append(
            Repair(
                resource_key=node.label,
                rule=finding.rule,
                reason=ended(f"Automatic repair of a problem HQ found: {finding.rule}"),
            )
        )
        if len(repairs) >= limit:
            break
    return tuple(repairs)


def _unreachable_kinds(topology: Topology) -> frozenset[str]:
    """Kinds governed only by abilities whose connection is not answering.

    Traversed rather than assumed: connection -> enables -> ability -> governs
    -> kind. A kind with no reachable path to a live connection cannot be
    repaired right now, and offering to try is the offer that fails later.
    """

    status_of = {node.id: node.status for node in topology.nodes}
    live_abilities = {
        edge.target for edge in topology.edges if edge.kind == "enables" and status_of.get(edge.source) != "serious"
    }
    governed_by: dict[str, set[str]] = {}
    kinds_of = {node.id: node.kind_key for node in topology.nodes}
    for edge in topology.edges:
        if edge.kind != "governs":
            continue
        kind_key = kinds_of.get(edge.target, "")
        if kind_key:
            governed_by.setdefault(kind_key, set()).add(edge.source)
    return frozenset(
        kind_key for kind_key, abilities in governed_by.items() if abilities and not (abilities & live_abilities)
    )
