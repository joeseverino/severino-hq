"""Claims about the estate, with the evidence behind them and what to run.

A finding is an answer arriving without being asked. A resource can be wrong
while every surface reads healthy, so the rules are about the shapes a silence
can take, each derivable from the projection alone, which is why an extension
gets them without the host learning what it is. Deriving is pure, and a remedy
is a registered capability, absent for whoever cannot run it (``_resolved``).
"""

import inspect
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import cache, partial
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
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
    delivery_findings,
    dns_findings,
    docker_estate,
    filesystem_findings,
    github_findings,
    machine_findings,
    perimeter_findings,
    posture,
    reboot_findings,
    record_findings,
    reference_findings,
    registration_findings,
    render_status_findings,
    repository_findings,
    tailnet_device_findings,
    tailnet_findings,
    unit_findings,
)
from .action_links import (
    ActionLink,
    action_with_return,
    command_action,
    read_link,
    read_now_link,
    topology_investigation_links,
)
from .answer_fields import Pinned, SchemaVersion
from .contracts import Effect, route_url
from .derivations import derivation, present
from .derived_inputs import FINDINGS_READS, estate_variant
from .describe import Describe, listed
from .finding_model import (
    CHECK_AGAIN,
    PLAYBOOK_INVESTIGATION,
    Finding,
    FindingEstate,
    FindingRemedy,
    FindingRule,
    Gathered,
    is_observable,
    parse_stamp,
)
from .finding_steps import Do, Press, Run, WrittenCheck, WrittenReason, WrittenSteps
from .first_seen import recorded
from .integration_validation import required_capability_names
from .integrations import held_graph
from .projection import MAX_PAGE_SIZE, QUERY, projection_scope, read_once
from .security import Principal
from .tone import Tone
from .topology import derive_topology
from .topology_model import (
    JOINED_KINDS,
    Topology,
    TopologyNode,
)
from .ui import ended
from .url_query import with_query
from .workflows import WrittenAction, claim_identity
lazy from . import automation_runs, cadence, capabilities
lazy from .domains import resources_of
lazy from .estate import estate_reading
lazy from .gathered_findings import gather_findings
lazy from .moments import ago
lazy from .record_saves import row
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


def _estate(topology: Topology, principal: Principal | None = None) -> FindingEstate:
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
        principal,
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
    posture,
    automation_findings,
    delivery_findings,
    machine_findings,
    record_findings,
    repository_findings,
)
RULES: tuple[FindingRule, ...] = tuple(rule for module in RULE_MODULES for rule in module.RULES)

_RULE_BY_NAME = {rule.name: rule for rule in RULES}
if len(_RULE_BY_NAME) != len(RULES):
    raise ValueError("Two finding modules use the same rule name.")


def finding_steps(finding: Finding, *, pressed: bool = False) -> tuple[Run | Do, ...]:
    """What a person does to resolve it: its own steps, else, where nothing
    is ``pressed`` either, its rule's ``operator_action`` in words.

    A detector names its remedies and never a press, whose link is the
    reader's to be offered or not (``_presses``), so one that writes a press
    is refused."""

    told = tuple(step for step in finding.steps if not isinstance(step, Press))
    if len(told) < len(finding.steps):
        raise ImproperlyConfigured(f"A finding of {finding.rule!r} carries a press; its detector names remedies.")
    found = _RULE_BY_NAME.get(finding.rule)
    return told or ((Do(found.operator_action),) if found and not pressed else ())


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


def _permitted(capability: str, principal: Principal) -> tuple[bool, Effect | None]:
    """Whether this principal may run it, and what the registry says it does."""

    spec = capabilities.capability_spec(capability)
    if spec is not None:
        return spec.permitted_to(principal), spec.effect
    # A rule naming a capability the registry does not hold is a contract error
    # the suite catches; at runtime the honest answer is to offer nothing.
    return False, None


def _playbooks(
    finding: Finding, subject: TopologyNode | None, principal: Principal
) -> tuple[list[FindingRemedy], list[ActionLink]]:
    """Adopted fixes and read-only investigations for this rule and machine."""

    remedies: list[FindingRemedy] = []
    investigations: list[ActionLink] = []
    if subject is None or subject.kind != MACHINE_KIND:
        return remedies, investigations
    machine = normalized_hostname(subject.label)
    for manifest in automation_runs.playbooks_for(finding.rule, machine):
        name = automation_runs.capability_name(manifest.id)
        if finding.rule not in manifest.investigates:
            # A fix is run as it stands, so one whose run needs an input of
            # its own is the press of whatever supplies that input.
            if automation_runs.asks_nothing(manifest):
                url = automation_runs.run_here_url(manifest.id, (machine,))
                remedies.append(FindingRemedy(capability=name, target="", url=url))
            continue
        allowed, _ = _permitted(name, principal)
        if not allowed:
            continue
        reachable = read_once(f"automation.reachable:{manifest.id}", partial(automation_runs.reachable, manifest))
        host = next((item for item in reachable if normalized_hostname(item) == machine), "")
        if host and (url := automation_runs.run_here_url(manifest.id, (host,), finding=finding_key(finding))):
            investigations.append(
                command_action(PLAYBOOK_INVESTIGATION, name, url, intent=capabilities.command_spec(name).label)
            )
    return remedies, investigations


def _presses(
    remedies: Iterable[FindingRemedy], principal: Principal, subject: TopologyNode | None
) -> tuple[Press, ...]:
    """Each remedy this principal may run, as the press that runs it, each route once.

    Absent rather than disabled: an offer that cannot work is worse than no
    offer. The effect is the capability registry's rather than the rule's, so
    a capability that becomes destructive tomorrow is never offered tomorrow
    without anyone editing a rule.
    """

    pressed: dict[tuple[str, str], Press] = {}
    for remedy in remedies:
        allowed, effect = _permitted(remedy.capability, principal)
        if not allowed or effect is None or effect is Effect.DESTRUCTIVE:
            continue
        action = next(
            (
                candidate
                for candidate in (subject.actions if subject else ())
                if candidate.capability == remedy.capability and candidate.target == remedy.target
            ),
            None,
        )
        url, method = (
            (action_with_return(action, "control_plane:findings").url, action.method) if action else remedy.reach
        )
        if url:
            link = command_action(
                "remedy",
                remedy.capability,
                url,
                method=method,
                target=remedy.target,
                variant=remedy.variant,
                intent=remedy.intent,
                recommended=True,
            )
            pressed.setdefault((link.method, link.url), Press(link))
    return tuple(pressed.values())


def _resolved(finding: Finding, principal: Principal, subject: TopologyNode | None) -> Finding:
    """What derivation adds to a detector's finding: its remedies as the
    presses this principal may make, then its steps, its reason, its check and
    the pages about its subject."""

    remedies, diagnostics = _playbooks(finding, subject, principal)
    subject_reads = tuple(
        action
        for action in (subject.actions if subject else ())
        if action.effect == Effect.READ and action.method == "GET"
    )
    investigations = topology_investigation_links(subject.id) if subject else ()
    presses = _presses((*finding.remedies, *remedies), principal, subject)
    check, read_now = _verification(finding, principal, subject)
    # Every field the detector set carries through; only what derivation adds changes.
    return replace(
        finding,
        remedies=(),
        offers=(*finding.offers, *diagnostics, *subject_reads),
        investigations=investigations,
        steps=(*presses, *finding_steps(finding, pressed=bool(presses))),
        no_help_reason=_no_help_reason(finding),
        check=check,
        subject_link=read_link("subject", subject.label, subject.url) if subject and subject.url else None,
        read_capabilities=_held_to(finding, subject, principal),
        read_now=read_now,
        # Where no one reading dates it, its subject's last read does.
        observed_at=finding.observed_at or (parse_stamp(subject.observed_at) if subject else None),
    )


# The resource each part of a finding is read through, whose declared
# capabilities are that part's read gate: a topology node through the
# relationships around it, a declared kind through its records, an observed
# kind through its readings, a rule through its ``read_through``.
NODE_READ = "relationships"
_RECORDS_READ = "infrastructure"
_READINGS_READ = "readings"


def read_gate(finding: Finding, subject: TopologyNode | None) -> tuple[str, ...] | None:
    """What a reader must hold to be told ``finding``; None when it cannot be derived.

    The union of its subject node's gate, each scope's, its rule's section's
    and ``read_through``'s, and what its detector carried for the evidence it
    names. A subject that names no node, a scope no resource reads, or a claim
    about nothing that carries no gate leaves it underivable.
    """

    scopes = _finding_scopes(finding)
    declared = _RULE_BY_NAME.get(finding.rule)
    section, through = (declared.domain, declared.read_through) if declared is not None else ("", "")
    if (finding.subject and subject is None) or not (
        subject or scopes or finding.read_capabilities or section or through
    ):
        return None
    read = (*resources_of(section), *([through] if through else []))
    gates = [finding.read_capabilities, *(_resource_gate(name) for name in read)]
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
    if scope in OBSERVATIONS:
        return _READINGS_READ
    if scope in PROVIDERS:
        return _RECORDS_READ
    return None


def _resource_gate(name: str) -> tuple[str, ...]:
    return required_capability_names(held_graph().resources[name])


def _no_help_reason(finding: Finding) -> str:
    """The finding's own reason, else its rule's, when it has no remedy to
    offer and no exact command to name. Judged before a principal's remedies
    are filtered: a remedy this reader may not run is still a remedy."""

    if finding.remedies or any(isinstance(step, Run) for step in finding.steps):
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
    again = with_query(findings_url, {"rule": finding.rule})
    if read is not None:
        verify = command_action(
            "verify",
            read.capability,
            with_query(read.url, {"next": again}),
            method="POST",
            target=read.target,
            intent=CHECK_AGAIN,
            reason="Reads it now, then checks again.",
        )
        return verify, wanted
    verify = read_link("verify", CHECK_AGAIN, again, reason="Runs this check against current data.")
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


def derive_findings(
    topology: Topology, *, principal: Principal, rule: str = "", gathered: Gathered | None = None
) -> tuple[Finding, ...]:
    """Every claim the projection and what was ``gathered`` support, most serious first.

    Pure: no query, no write. The projection was already narrowed to what this
    principal may see, so a finding cannot reveal a node the caller could not
    already read.
    """

    estate = _estate(topology, principal)
    wanted = _RULE_BY_NAME.get(rule) if rule else None
    raised: dict[str, tuple[Finding, ...]] = {}
    found = gathered or {}
    for declared in RULES:
        raised[declared.name] = found.get(declared.name, ()) if declared.gathered else declared.detect(estate)

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

    return tuple(sorted(findings, key=lambda f: (f.severity.rank, f.rule, f.subject)))


def estate_findings(*, principal: Principal, rule: str = "") -> tuple[Any, ...]:
    """Every finding the topology ``principal`` may see supports, in one
    projection, so the topology and the gathered rules share each read."""

    with projection_scope():
        return _estate_findings(principal, rule)


@derivation(
    "estate.findings",
    reads=FINDINGS_READS,
    vary=lambda principal, rule: (estate_variant(principal), rule),
)
def _estate_findings(principal: Principal, rule: str) -> tuple[Any, ...]:
    topology = derive_topology(principal=principal)
    # A name no rule has asks for every finding, as derivation reads it.
    asked = rule if rule in _RULE_BY_NAME else ""
    gathered = gather_findings(_estate(topology, principal), principal=principal, rule=asked)
    raised = derive_findings(topology, principal=principal, rule=rule, gathered=gathered)
    seen = recorded() if raised else {}
    # A notice says when in its own words; a detector may read when its condition began.
    return tuple(
        replace(finding, since=None if finding.notice else finding.since or seen.get(finding_key(finding)))
        for finding in raised
    )


def finding_key(finding: Finding) -> str:
    """What a finding is about, as its item in the queue is keyed."""

    about = f"finding:{finding.rule}:{finding.subject or finding.scope}"
    # Two containers on one machine are two matters, each keyed by its own.
    return f"{about}:{finding.part}" if finding.part else about


def finding_id(finding: Finding) -> str:
    """A finding's id where an answer names it, derived from its key alone."""

    return claim_identity(_CLAIM_NAMESPACE, finding_key(finding))


def rule_of(item_key: str) -> str:
    """The rule a queue item's key names when the item is a finding, read
    back from ``finding_key`` behind its source; empty for any other item."""

    _, found, rest = item_key.partition("finding:")
    return rest.split(":", 1)[0] if found else ""


class FindingEvidence(TypedDict):
    """One line of what a finding rests on."""

    label: Annotated[str, Describe("What this line of evidence is, as a problem card labels it.")]
    value: Annotated[Any, Describe("What was found, most often a name or a sentence.")]


class FindingFacts(TypedDict):
    """What a finding says the same at every door: what it is about, what it
    rests on, and why HQ cannot help where it cannot. Each door adds its steps
    and actions."""

    id: Annotated[
        str,
        Describe("The problem's id, the same at every read while it is open."),
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
    severity: Annotated[str, Describe(f"How serious it is: {listed(Tone)}, most severe first.")]
    explanation: Annotated[str, Describe("Why it matters and what it rests on, in sentences.")]
    evidence: Annotated[list[FindingEvidence], Describe("What it rests on, a labeled line each.")]
    no_help_reason: WrittenReason
    since: Annotated[
        str | None,
        Describe("When it was first seen open, an ISO 8601 instant; null where no read has recorded it."),
    ]


class SerializedFinding(FindingFacts):
    """One finding as the API answers it; MCP's `get_findings` makes its actions tool calls."""

    steps: WrittenSteps
    check: WrittenCheck
    offers: Annotated[list[WrittenAction], Describe("Reads of its subject this reader may run before acting.")]
    investigations: Annotated[
        list[WrittenAction],
        Describe("Where on the map to look around its subject: its neighborhood and what it affects."),
    ]


def _computed(finding: Finding) -> dict[str, Any]:
    return {
        "id": finding_id(finding),
        "evidence": [{"label": label, "value": value} for label, value in finding.evidence],
    }


def finding_facts(finding: Finding) -> FindingFacts:
    return row(finding, FindingFacts, **_computed(finding))


def serialize_finding(finding: Finding) -> SerializedFinding:
    return row(finding, SerializedFinding, **_computed(finding))


# Built from the declarations, so a type checker reads each as a value.
_RULE: Any = Literal[tuple(rule.name for rule in RULES)]
_SEVERITY: Any = Literal[tuple(tone.value for tone in Tone)]

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
            le=MAX_PAGE_SIZE,
            description=(
                f"How many problems to return, 1 to {MAX_PAGE_SIZE}, most severe first; the summary still counts "
                "every one."
            ),
        ),
    ),
)


# The shape findings answers in; it moves when that shape does.
FINDINGS_SCHEMA_VERSION = 3


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

    The summary still counts every finding, and says whether ``limit`` held
    some back.
    """

    query = FindingsQuery.model_validate(narrowing).model_dump()
    rule: str = query["rule"] or ""
    limit: int | None = query["limit"]
    floor = Tone(query["severity"]).rank if query["severity"] else len(Tone)
    raised = estate_findings(principal=principal, rule=rule)
    counts = Counter(finding.severity for finding in raised)
    eligible = [finding for finding in raised if finding.severity.rank <= floor]
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
        if finding.rule not in _AUTO_RULES or not finding.presses:
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
        edge.target for edge in topology.edges if edge.kind == "enables" and status_of.get(edge.source) != Tone.SERIOUS
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
