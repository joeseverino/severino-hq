"""Claims about the estate, with the evidence behind them and what to run.

A lens is a question an operator has to think to ask. A finding is the answer
arriving without being asked, and that difference is the whole of this module.

The bug it was written for looked like nothing at all. A provider blanked a
field it declared, so a declaration compared unequal to the world forever, so
the sweep correctly refused to call it observed, and the resource went on
reporting the condition its last reconcile wrote. Health said healthy. The
declared and observed revisions matched, so nothing queued a reconcile. The one
fact that moved was ``last_observed_at`` falling behind its siblings, and
nothing read it. Two of the most important hosts in the estate were unverified
for days and every surface said they were fine.

So the rules here are deliberately not about certificates or proxies. They are
about the shapes a silence can take: observed later than everything of its own
kind, never observed at all, asked for but never confirmed, reconciled again
and again against a world that keeps disagreeing. Each is derivable from the
projection alone (kinds, edges, two revisions, an age, a reason) which is
why an extension gets them without the host learning what it is.

Three properties hold and are tested:

Derivation is pure. Nothing here queries, and nothing here mutates. A finding
is computed from a projection that was already derived and already authorized,
so deriving one cannot widen what a principal can see, and rendering a page
cannot change the estate.

A remedy is a reference, never a route. It names a capability already in the
registry and a target, and it copies that capability's effect and required
permissions rather than restating them, so a capability that becomes
destructive tomorrow is reported as destructive tomorrow. Executing one is a
call to the capability the caller was always going to call.

Absence of a remedy is a fact, not an omission. A principal who cannot run the
capability sees the finding and the evidence and no remedy at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from django.utils import timezone

from control_plane.providers import PROVIDERS

from . import (
    certificate_expiry,
    connection_findings,
    controller_findings,
    credential_findings,
    dns_findings,
    docker_estate,
    perimeter_findings,
    registration_findings,
    tailnet_findings,
)
from .action_links import (
    ActionLink,
    action_with_return,
    read_now_link,
    topology_investigation_links,
)
from .integrations import IntegrationGraph, integration_graph
from .contracts import route_url
from .finding_model import OperatorStep
from .security import Principal
from .topology import derive_topology
from .topology_model import (
    JOINED_KINDS,
    Topology,
    TopologyNode,
)
from .workflows import (
    WorkflowLayout,
    claim_identity,
    claim_resolution_plan,
    serialize_workflow,
    workflow_layout,
)
from .finding_model import Finding, FindingRule, Remedy, FindingEstate, is_observable
from .finding_model import parse_stamp
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


def _controllers_by_kind(
    topology: Topology, by_id: dict[str, TopologyNode]
) -> dict[str, frozenset[str]]:
    """Controllers a kind can be attributed to without guessing."""

    indexed = _causal_edges(topology)
    controllers_by_kind: dict[str, set[str]] = {}
    for resource_id, connections in indexed["used_by"].items():
        kind = getattr(by_id.get(resource_id), "kind_key", "")
        controllers = {
            controller
            for connection in connections
            for controller in indexed["carries"].get(connection, set())
        }
        if kind and controllers:
            controllers_by_kind.setdefault(kind, set()).update(controllers)

    # Ability nodes are shared by a connection family. They prove a cause only
    # when exactly one controller enables that ability; otherwise attributing
    # every governed resource to both would manufacture knowledge HQ lacks.
    for ability, connections in indexed["enables"].items():
        controllers = {
            controller
            for connection in connections
            for controller in indexed["carries"].get(connection, set())
        }
        if len(controllers) != 1:
            continue
        for resource_id in indexed["governs"].get(ability, set()):
            kind = getattr(by_id.get(resource_id), "kind_key", "")
            if kind:
                controllers_by_kind.setdefault(kind, set()).update(controllers)
    return {kind: frozenset(items) for kind, items in controllers_by_kind.items()}


def _estate(topology: Topology) -> FindingEstate:
    observed: dict[str, datetime] = {}
    latest: dict[str, datetime] = {}
    for node in topology.nodes:
        moment = parse_stamp(node.observed_at)
        if moment is None or not node.kind_key or node.kind in JOINED_KINDS:
            continue
        observed[node.id] = moment
        newest = latest.get(node.kind_key)
        if newest is None or moment > newest:
            latest[node.kind_key] = moment
    governed = frozenset(
        edge.target for edge in topology.edges if edge.kind == "governs"
    )
    counts: dict[str, int] = {}
    for node in topology.nodes:
        if node.kind == "resource" and node.managed and node.kind_key:
            counts[node.kind_key] = counts.get(node.kind_key, 0) + 1
    by_id = {node.id: node for node in topology.nodes}
    return FindingEstate(
        topology,
        timezone.now(),
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
    perimeter_findings,
    connection_findings,
    credential_findings,
    certificate_expiry,
    registration_findings,
    controller_findings,
    tailnet_findings,
    dns_findings,
)
RULES: tuple[FindingRule, ...] = tuple(
    rule for module in RULE_MODULES for rule in module.RULES
)

_RULE_BY_NAME = {rule.name: rule for rule in RULES}
if len(_RULE_BY_NAME) != len(RULES):
    raise ValueError("Two finding modules declare the same rule name.")


def finding_steps(finding: Finding) -> tuple[OperatorStep, ...]:
    """What a person does to resolve it: its own steps, else its rule's."""

    if finding.steps:
        return finding.steps
    found = _RULE_BY_NAME.get(finding.rule)
    return (OperatorStep(label=found.operator_action),) if found else ()


def finding_layout(finding: Finding) -> WorkflowLayout:
    """How a card shows a finding's actions: its plan's, or, for a finding built
    without one, its own investigations and offers as links."""

    if finding.workflow is not None:
        return workflow_layout(finding.workflow)
    return WorkflowLayout(impact=finding.investigations, related=finding.offers)


def finding_rules() -> tuple[FindingRule, ...]:
    """Every claim HQ knows how to make about itself."""

    return RULES


def rule_for(name: str) -> FindingRule | None:
    return _RULE_BY_NAME.get(name)


def _permitted(
    capability: str, principal: Principal, graph: IntegrationGraph
) -> tuple[bool, str]:
    """Whether this principal may run it, and what the registry says it does."""

    spec = graph.capabilities.get(capability)
    if spec is not None:
        return principal.permits(*spec.required_capabilities), spec.effect
    # A rule naming a capability the registry does not hold is a contract error
    # the suite catches; at runtime the honest answer is to offer nothing.
    return False, ""


def _resolved(
    finding: Finding, principal: Principal, subject: TopologyNode | None
) -> Finding:
    """Drop remedies this principal cannot run, and take effect from the spec.

    Absent rather than disabled: an offer that cannot work is worse than no
    offer. The effect is copied from the capability registry rather than
    restated by the rule, so a capability that becomes destructive tomorrow is
    described as destructive tomorrow without anyone editing a rule.
    """

    graph = integration_graph()
    kept = []
    for remedy in finding.remedies:
        allowed, effect = _permitted(remedy.capability, principal, graph)
        if not allowed or effect == "destructive":
            continue
        action = next(
            (
                candidate
                for candidate in (subject.actions if subject else ())
                if candidate.capability == remedy.capability
                and candidate.target == remedy.target
            ),
            None,
        )
        kept.append(
            Remedy(
                capability=remedy.capability,
                target=remedy.target,
                label=remedy.label,
                effect=effect,
                url=(
                    action_with_return(action, "control_plane:findings").url
                    if action
                    else remedy.url
                ),
                method=action.method if action else remedy.method,
                auto=False,
            )
        )
    resolved_remedies = tuple(kept)
    offers = tuple(
        action
        for action in (subject.actions if subject else ())
        if action.effect == "read" and action.method == "GET"
    )
    investigations = topology_investigation_links(subject.id) if subject else ()
    remedy_actions = tuple(
        ActionLink(
            "remedy",
            remedy.label,
            remedy.effect,
            remedy.url,
            method=remedy.method,
            capability=remedy.capability,
            target=remedy.target,
            recommended=True,
        )
        for remedy in resolved_remedies
        if remedy.url
    )
    verification = _verification(finding, principal, subject)
    workflow = claim_resolution_plan(
        namespace=_CLAIM_NAMESPACE,
        rule=finding.rule,
        subject=finding.subject,
        scope=finding.scope,
        investigations=investigations,
        offers=offers,
        remedies=remedy_actions,
        verification=verification,
    )
    return Finding(
        rule=finding.rule,
        subject=finding.subject,
        title=finding.title,
        severity=finding.severity,
        explanation=finding.explanation,
        evidence=finding.evidence,
        remedies=resolved_remedies,
        offers=offers,
        investigations=investigations,
        scope=finding.scope,
        affected_scopes=finding.affected_scopes,
        workflow=workflow,
        steps=finding_steps(finding),
    )


def _read_subject(finding: Finding, subject: TopologyNode | None) -> dict[str, Any] | None:
    """What to read now so the check re-runs on fresh data; None when no reading
    is involved."""

    from control_plane.observations import OBSERVATIONS

    if subject is not None and subject.kind == "connection" and subject.connection_ref:
        return {"connection_ref": subject.connection_ref}
    kind = finding.scope or (subject.kind_key if subject is not None else "")
    if kind in OBSERVATIONS or (kind in PROVIDERS and is_observable(kind)):
        return {"kind": kind}
    if finding.affected_scopes:
        return {"every_connection": True}
    return None


def _verification(
    finding: Finding, principal: Principal, subject: TopologyNode | None
) -> ActionLink | None:
    """Read now and check again where a reading is involved; else check again."""

    findings_url = route_url("control_plane:findings")
    if not findings_url:
        return None
    wanted = _read_subject(finding, subject)
    read = (
        read_now_link(principal, label="Read now and check again", **wanted)
        if wanted is not None
        else None
    )
    again = f"{findings_url}?{urlencode({'rule': finding.rule})}"
    if read is not None:
        separator = "&" if "?" in read.url else "?"
        return ActionLink(
            "verify",
            read.label,
            read.effect,
            f"{read.url}{separator}{urlencode({'next': again})}",
            method="POST",
            capability=read.capability,
            target=read.target,
            reason="Reads it now; the check runs again on what the controller reports.",
        )
    return ActionLink(
        "verify",
        "Check again",
        "read",
        again,
        reason="Runs this check against current data.",
    )


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
        scopes = {
            scope
            for finding in raised[declared.name]
            for scope in _finding_scopes(finding)
        }
        for name in declared.subsumes:
            silenced.setdefault(name, set()).update(scopes)
    return silenced


def _subsumed_subjects(
    raised: dict[str, tuple[Finding, ...]],
) -> dict[str, set[str]]:
    subjects: dict[str, set[str]] = {}
    for declared in RULES:
        found = {
            finding.subject
            for finding in raised[declared.name]
            if finding.subject
        }
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
    topology: Topology, *, principal: Principal, rule: str = ""
) -> tuple[Finding, ...]:
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

    order = {"serious": 0, "attention": 1, "neutral": 2, "good": 3}
    return tuple(
        sorted(findings, key=lambda f: (order.get(f.severity, 9), f.rule, f.subject))
    )


def serialize_finding(finding: Finding) -> dict[str, Any]:
    return {
        "id": claim_identity(
            _CLAIM_NAMESPACE, finding.rule, finding.subject, finding.scope
        ),
        "rule": finding.rule,
        "subject": finding.subject or None,
        "scope": finding.scope or None,
        "affected_scopes": list(finding.affected_scopes),
        "title": finding.title,
        "severity": finding.severity,
        "explanation": finding.explanation,
        "evidence": [
            {"label": label, "value": value} for label, value in finding.evidence
        ],
        "remedies": [
            {
                "capability": remedy.capability,
                "target": remedy.target,
                "label": remedy.label,
                "effect": remedy.effect,
                "auto": remedy.auto,
                "method": "POST",
                "url": f"/api/v2/capabilities/{remedy.capability}/",
            }
            for remedy in finding.remedies
        ],
        "offers": [asdict(action) for action in finding.offers],
        "investigations": [asdict(action) for action in finding.investigations],
        "workflow": serialize_workflow(finding.workflow),
        "operator_steps": [asdict(step) for step in finding.steps],
    }


def findings(*, principal: Principal, rule: str = "") -> dict[str, Any]:
    """The serialized claims, for machine delivery adapters."""

    selected = rule_for(rule) if rule else None
    raised = derive_findings(
        derive_topology(principal=principal),
        principal=principal,
        rule=selected.name if selected else "",
    )
    counts: dict[str, int] = {}
    for finding in raised:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return {
        "ok": True,
        "schema_version": 2,
        # Which rule produced this, and every rule that could have. A client
        # that asked for an unknown one is told it got everything.
        "rule": selected.name if selected else None,
        "rules": [
            {"name": item.name, "title": item.title, "severity": item.severity}
            for item in RULES
        ],
        "summary": {"findings": len(raised), "severities": counts},
        "findings": [serialize_finding(finding) for finding in raised],
    }


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
# HQ forms no opinion about what is safe to run unattended. That judgement is
# already written down, per kind and per action, in the controller contract,
# so the only actions considered here are the ones it already declares
# automatic, and withdrawing one there withdraws it here.

_AUTO_RULES = ("skipped-by-a-sweep", "never-observed")


@dataclass(frozen=True)
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

    from control_plane.providers import enabled_controller_actions
    from control_plane.models import OperationRequest

    automatic_kinds = {
        kind
        for kind, action in enabled_controller_actions(automatic_only=True)
        if action == OperationRequest.Action.RECONCILE
    }
    if not automatic_kinds:
        return ()

    topology = derive_topology(principal=principal)
    raised = derive_findings(topology, principal=principal)

    # A kind the sweep never reached: the fault is the sweep. Repairing each
    # record of it would queue the whole class against a provider that is not
    # answering, which is the amplifier this guard exists to prevent.
    unreached = {
        finding.scope for finding in raised if finding.rule == "kind-never-swept"
    }
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
                reason=f"Automatic repair of a finding: {finding.rule}.",
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
        edge.target
        for edge in topology.edges
        if edge.kind == "enables" and status_of.get(edge.source) != "serious"
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
        kind_key
        for kind_key, abilities in governed_by.items()
        if abilities and not (abilities & live_abilities)
    )
