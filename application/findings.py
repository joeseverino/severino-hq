"""Claims about the estate, with the evidence behind them and what to run.

A lens is a question an operator has to think to ask. A finding is the answer
arriving without being asked, and that difference is the whole of this module.

The bug it was written for looked like nothing at all. A provider blanked a
field it declared, so a declaration compared unequal to the world forever, so
the sweep correctly refused to call it observed -- and the resource went on
reporting the condition its last reconcile wrote. Health said healthy. The
declared and observed revisions matched, so nothing queued a reconcile. The one
fact that moved was ``last_observed_at`` falling behind its siblings, and
nothing read it. Two of the most important hosts in the estate were unverified
for days and every surface said they were fine.

So the rules here are deliberately not about certificates or proxies. They are
about the shapes a silence can take: observed later than everything of its own
kind, never observed at all, asked for but never confirmed, reconciled again
and again against a world that keeps disagreeing. Each is derivable from the
projection alone -- kinds, edges, two revisions, an age, a reason -- which is
why an extension gets them without the host learning what it is.

Three properties hold and are tested:

Derivation is pure. Nothing here queries, and nothing here mutates. A finding
is computed from a projection that was already derived and already authorized,
so deriving one cannot widen what a principal can see, and rendering a page
cannot change the estate.

A remedy is a reference, never a route. It names a capability already in the
registry and a target, and it copies that capability's effect and required
permissions rather than restating them -- so a capability that becomes
destructive tomorrow is reported as destructive tomorrow. Executing one is a
call to the capability the caller was always going to call.

Absence of a remedy is a fact, not an omission. A principal who cannot run the
capability sees the finding and the evidence and no remedy at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from django.utils import timezone

from .capabilities import capability_specs
from .cadence import sweep_interval
from .security import AuthorizationError, Principal
from .topology import (
    _STALE_AFTER,
    Topology,
    TopologyNode,
    derive_topology,
)


# ``_STALE_AFTER`` is imported rather than restated: the lens that asks "what
# was left behind?" and the rule that claims it must not be able to disagree
# about where that line falls.

# How many sweep intervals a whole kind may go unobserved before the fault is
# the sweep rather than any one record. Three, because one missed tick is a
# restart and two is a slow provider.
_KIND_SILENT_AFTER = 3


@dataclass(frozen=True)
class Remedy:
    """An existing capability, named -- never a new way to change anything."""

    capability: str
    target: str
    label: str
    effect: str
    url: str = ""
    # Whether the controller would run this unattended. Left False here: this
    # module proposes, and the thing that already schedules automatic work is
    # the only correct place for anything else.
    auto: bool = False


@dataclass(frozen=True)
class Finding:
    """One claim, the evidence for it, and what could be done about it."""

    rule: str
    subject: str
    title: str
    severity: str
    explanation: str
    evidence: tuple[tuple[str, str], ...] = ()
    remedies: tuple[Remedy, ...] = ()
    # A kind rather than a node, for a claim about a whole class. The estate
    # keeps its honesty: no synthetic node is invented to hang this on.
    scope: str = ""


@dataclass(frozen=True)
class FindingRule:
    """A named claim, and how to decide and explain it.

    ``subsumes`` is what keeps a queue from tripling. A resource nothing governs
    is uncovered, not skipped, and reporting both puts one problem in front of
    an operator twice under two names.
    """

    name: str
    title: str
    severity: str
    detect: Callable[["_Estate"], tuple[Finding, ...]]
    subsumes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Estate:
    """The projection plus the two indices every rule wants, built once."""

    topology: Topology
    now: datetime
    latest_by_kind: dict[str, datetime]
    observed: dict[str, datetime]
    governed: frozenset[str]
    # Every kind that has a managed declaration, and how many. A kind absent
    # from `latest_by_kind` but present here has never been observed at all.
    declared_kinds: frozenset[str]
    declared_counts: dict[str, int]

    def nodes(self) -> tuple[TopologyNode, ...]:
        return self.topology.nodes


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _estate(topology: Topology) -> _Estate:
    observed: dict[str, datetime] = {}
    latest: dict[str, datetime] = {}
    for node in topology.nodes:
        moment = _parse(node.observed_at)
        if moment is None or not node.kind_key:
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
    return _Estate(
        topology,
        timezone.now(),
        latest,
        observed,
        governed,
        frozenset(counts),
        counts,
    )


def _ago(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _reconcile(node: TopologyNode) -> tuple[Remedy, ...]:
    """The one capability that answers "go and look again"."""

    return (
        Remedy(
            capability="infrastructure.reconcile",
            target=node.label,
            label="Reconcile",
            effect="",
        ),
    )


def _skipped_by_a_sweep(estate: _Estate) -> tuple[Finding, ...]:
    """Observed materially later than everything else of its own kind.

    A sweep confirms everything it matches in one pass and writes one timestamp,
    so siblings land together. One left behind was not slow, it was skipped --
    and being skipped is invisible in every other surface, because the thing
    keeps whatever it last said about itself.
    """

    found = []
    for node in estate.nodes():
        moment = estate.observed.get(node.id)
        newest = estate.latest_by_kind.get(node.kind_key)
        if moment is None or newest is None or not node.managed:
            continue
        behind = newest - moment
        if behind <= _STALE_AFTER:
            continue
        siblings = sum(
            1
            for other in estate.nodes()
            if other.kind_key == node.kind_key and other.id in estate.observed
        )
        found.append(
            Finding(
                rule="skipped-by-a-sweep",
                subject=node.id,
                title=f"{node.label} was not confirmed by the last sweep",
                severity="serious",
                explanation=(
                    f"A sweep confirmed other {node.kind_key} records "
                    f"{_ago(behind)} more recently than this one. It ran and "
                    "did not match this declaration, which leaves the record "
                    "reporting whatever it last said about itself."
                ),
                evidence=(
                    ("Last observed", node.observed_at),
                    ("Newest of this kind", newest.isoformat()),
                    ("Behind by", _ago(behind)),
                    ("Records of this kind observed", str(siblings)),
                    ("Condition reason", node.reason or "none"),
                ),
                remedies=_reconcile(node),
            )
        )
    return tuple(found)


def _kind_never_swept(estate: _Estate) -> tuple[Finding, ...]:
    """A whole kind that nothing has observed lately.

    The sibling comparison above cannot see this: a kind with one record has
    nothing to be behind, and a kind where every record is equally stale looks
    perfectly consistent. Ship the two together or the hole is still open --
    and this one is deliberately blunt, an absolute clock against the interval
    HQ itself declares, because that is the only thing left to compare against.
    """

    interval = sweep_interval() * _KIND_SILENT_AFTER
    found = []
    for kind_key, newest in sorted(estate.latest_by_kind.items()):
        silent = estate.now - newest
        if silent <= interval:
            continue
        found.append(
            Finding(
                rule="kind-never-swept",
                subject="",
                scope=kind_key,
                title=f"Nothing has observed {kind_key} for {_ago(silent)}",
                severity="serious",
                explanation=(
                    "Every record of this kind is equally stale, so the gap is "
                    "in the sweep rather than in any one declaration. Whatever "
                    "these records currently report is that old."
                ),
                evidence=(
                    ("Newest observation", newest.isoformat()),
                    ("Silent for", _ago(silent)),
                    ("Sweep interval", _ago(sweep_interval())),
                ),
            )
        )
    # A kind with no observation at all has no newest to be behind, so the loop
    # above cannot see it -- and left alone it becomes one finding per record.
    # Against a real estate that was three hundred and twenty claims saying the
    # same thing once each, which is how a queue stops being read. Said once
    # about the kind, it is one line and the same information.
    for kind_key in sorted(estate.declared_kinds - set(estate.latest_by_kind)):
        found.append(
            Finding(
                rule="kind-never-swept",
                subject="",
                scope=kind_key,
                title=f"Nothing has ever observed {kind_key}",
                severity="serious",
                explanation=(
                    "No record of this kind has ever been confirmed, so the "
                    "gap is in reaching the kind at all rather than in any one "
                    "declaration. Everything these records report is the "
                    "declaration talking about itself."
                ),
                evidence=(
                    ("Newest observation", "never"),
                    (
                        "Records of this kind",
                        str(estate.declared_counts.get(kind_key, 0)),
                    ),
                    ("Sweep interval", _ago(sweep_interval())),
                ),
            )
        )
    return tuple(found)


def _reconciled_but_still_wrong(estate: _Estate) -> tuple[Finding, ...]:
    """Converged on paper, disagreeing in practice.

    The two revisions match, so everything that asks "has anything changed?"
    answers no and nothing is queued -- while the status says otherwise. That
    combination means the reconcile already ran against this exact declaration
    and the world still disagrees, so running it again will not help. This is
    the one shape where the declaration itself is the suspect.
    """

    return tuple(
        Finding(
            rule="reconciled-but-still-wrong",
            subject=node.id,
            title=f"{node.label} keeps disagreeing after reconciling",
            severity="serious",
            explanation=(
                "The declared and observed revisions match, so nothing will "
                "queue another attempt, and the status is still not good. A "
                "reconcile has already been tried against this exact "
                "declaration, which points at the declaration rather than at "
                "the convergence."
            ),
            evidence=(
                ("Status", node.status_label or node.status),
                ("Declared revision", str(node.declared_revision)),
                ("Observed revision", str(node.observed_revision)),
                ("Condition reason", node.reason or "none"),
                ("Detail", node.detail or "none"),
            ),
        )
        for node in estate.nodes()
        if node.kind == "resource"
        and node.managed
        and node.status in {"attention", "serious"}
        and node.declared_revision == node.observed_revision
        and node.declared_revision > 0
    )


def _never_observed(estate: _Estate) -> tuple[Finding, ...]:
    """Declared, governed by something that could look, and never looked at.

    Gated twice on purpose. An inbound ``governs`` edge, because a resource
    nothing governs is uncovered rather than skipped -- a different finding with
    a different answer. And an observed sibling, because without one the whole
    kind is unreached and that belongs to ``kind-never-swept``, said once.
    """

    return tuple(
        Finding(
            rule="never-observed",
            subject=node.id,
            title=f"{node.label} has never been observed",
            severity="attention",
            explanation=(
                "An ability governs this kind, so something is able to look, "
                "and nothing ever has. Everything this record reports is the "
                "declaration talking about itself."
            ),
            evidence=(
                ("Last observed", "never"),
                ("Declared revision", str(node.declared_revision)),
                ("Observed revision", str(node.observed_revision)),
            ),
            remedies=_reconcile(node),
        )
        for node in estate.nodes()
        if node.kind == "resource"
        and node.managed
        and not node.observed_at
        and node.id in estate.governed
        # A sibling has been observed, so the sweep can see this kind and
        # missed this one. Without that, the gap is the kind's, and saying it
        # per record buries the one claim worth reading.
        and node.kind_key in estate.latest_by_kind
    )


def _weakly_verified(estate: _Estate) -> tuple[Finding, ...]:
    """Observed, and still asserting things the observation never confirmed.

    Drift is judged only across fields both sides carry, so a field the reading
    omits is not agreed -- it is unjudged. A record can therefore be confirmed,
    read healthy, and be asserting a control nothing has ever checked.

    That is not a hypothetical: the two proxy hosts that carried this estate's
    only `block_exploits` were also the two the sweep never confirmed, and what
    it did confirm about them was two fields out of seventeen. An unverified
    control is not a control, and staleness alone would not have said so.
    """

    return tuple(
        Finding(
            rule="weakly-verified",
            subject=node.id,
            title=f"{node.label} asserts {len(node.unconfirmed_fields)} unconfirmed field(s)",
            severity="attention",
            explanation=(
                "The last observation confirmed this record but said nothing "
                "about these fields, and drift is only judged where both sides "
                "speak. Whatever they assert has not been checked. Either the "
                "provider should report them, or it should declare that it "
                "cannot so the gap is a known one."
            ),
            evidence=(
                ("Unconfirmed", ", ".join(node.unconfirmed_fields)),
                ("Last observed", node.observed_at),
                ("Condition reason", node.reason or "none"),
            ),
            remedies=_reconcile(node),
        )
        for node in estate.nodes()
        if node.kind == "resource" and node.managed and node.unconfirmed_fields
    )


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "skipped-by-a-sweep",
        "Not confirmed by the last sweep",
        "serious",
        _skipped_by_a_sweep,
    ),
    FindingRule(
        "kind-never-swept",
        "A whole kind has gone unobserved",
        "serious",
        _kind_never_swept,
        # When the sweep itself is the fault, every record of the kind looks
        # skipped. Saying it once about the kind beats saying it about each.
        subsumes=("skipped-by-a-sweep", "never-observed"),
    ),
    FindingRule(
        "reconciled-but-still-wrong",
        "Still wrong after reconciling",
        "serious",
        _reconciled_but_still_wrong,
    ),
    FindingRule(
        "weakly-verified",
        "Asserting fields nothing confirmed",
        "attention",
        _weakly_verified,
    ),
    FindingRule(
        "never-observed",
        "Never observed",
        "attention",
        _never_observed,
    ),
)

_RULE_BY_NAME = {rule.name: rule for rule in RULES}


def finding_rules() -> tuple[FindingRule, ...]:
    """Every claim HQ knows how to make about itself."""

    return RULES


def rule_for(name: str) -> FindingRule | None:
    return _RULE_BY_NAME.get(name)


def _permitted(capability: str, principal: Principal) -> tuple[bool, str]:
    """Whether this principal may run it, and what the registry says it does."""

    for spec in capability_specs():
        if spec.name != capability:
            continue
        try:
            for required in spec.required_capabilities:
                principal.require(required)
        except AuthorizationError:
            return False, spec.effect
        return True, spec.effect
    # A rule naming a capability the registry does not hold is a contract error
    # the suite catches; at runtime the honest answer is to offer nothing.
    return False, ""


def _resolved(finding: Finding, principal: Principal) -> Finding:
    """Drop remedies this principal cannot run, and take effect from the spec.

    Absent rather than disabled: an offer that cannot work is worse than no
    offer. The effect is copied from the capability registry rather than
    restated by the rule, so a capability that becomes destructive tomorrow is
    described as destructive tomorrow without anyone editing a rule.
    """

    kept = []
    for remedy in finding.remedies:
        allowed, effect = _permitted(remedy.capability, principal)
        if not allowed or effect == "destructive":
            continue
        kept.append(
            Remedy(
                capability=remedy.capability,
                target=remedy.target,
                label=remedy.label,
                effect=effect,
                url=remedy.url,
                auto=False,
            )
        )
    return Finding(
        rule=finding.rule,
        subject=finding.subject,
        title=finding.title,
        severity=finding.severity,
        explanation=finding.explanation,
        evidence=finding.evidence,
        remedies=tuple(kept),
        scope=finding.scope,
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
    silenced_kinds = {
        finding.scope
        for declared in RULES
        for finding in raised[declared.name]
        if declared.subsumes and finding.scope
    }
    subsumed_by: dict[str, set[str]] = {}
    for declared in RULES:
        for name in declared.subsumes:
            subsumed_by.setdefault(name, set()).update(
                finding.subject for finding in raised[declared.name] if finding.subject
            )

    by_id = {node.id: node for node in topology.nodes}
    findings = []
    for declared in RULES:
        if wanted is not None and declared.name != wanted.name:
            continue
        for finding in raised[declared.name]:
            if finding.subject in subsumed_by.get(declared.name, set()):
                continue
            node = by_id.get(finding.subject)
            if node is not None and node.kind_key in silenced_kinds:
                continue
            findings.append(_resolved(finding, principal))

    order = {"serious": 0, "attention": 1, "neutral": 2, "good": 3}
    return tuple(
        sorted(findings, key=lambda f: (order.get(f.severity, 9), f.rule, f.subject))
    )


def _serialize(finding: Finding) -> dict[str, Any]:
    return {
        "rule": finding.rule,
        "subject": finding.subject or None,
        "scope": finding.scope or None,
        "title": finding.title,
        "severity": finding.severity,
        "explanation": finding.explanation,
        "evidence": [{"label": label, "value": value} for label, value in finding.evidence],
        "remedies": [
            {
                "capability": remedy.capability,
                "target": remedy.target,
                "label": remedy.label,
                "effect": remedy.effect,
                "auto": remedy.auto,
                "url": f"/api/v2/capabilities/{remedy.capability}/",
            }
            for remedy in finding.remedies
        ],
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
        "schema_version": 1,
        # Which rule produced this, and every rule that could have. A client
        # that asked for an unknown one is told it got everything.
        "rule": selected.name if selected else None,
        "rules": [
            {"name": item.name, "title": item.title, "severity": item.severity}
            for item in RULES
        ],
        "summary": {"findings": len(raised), "severities": counts},
        "findings": [_serialize(finding) for finding in raised],
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
# already written down, per kind and per action, in the controller contract --
# so the only actions considered here are the ones it already declares
# automatic, and withdrawing one there withdraws it here.

_AUTO_RULES = ("skipped-by-a-sweep", "never-observed")


@dataclass(frozen=True)
class Repair:
    """One finding judged safe to queue, and why."""

    resource_key: str
    rule: str
    reason: str


def auto_remediable(
    *, principal: Principal, limit: int = 10
) -> tuple[Repair, ...]:
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
