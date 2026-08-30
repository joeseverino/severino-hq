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

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Callable
from urllib.parse import urlencode

from django.utils import timezone

from .action_links import ActionLink, action_with_return, topology_investigation_links
from .capabilities import capability_specs
from .cadence import sweep_interval
from .contracts import route_url
from .security import AuthorizationError, Principal
from .topology import (
    _STALE_AFTER,
    Topology,
    TopologyNode,
    derive_topology,
)
from .workflows import (
    WorkflowPlan,
    claim_identity,
    claim_resolution_plan,
    serialize_workflow,
)


# ``_STALE_AFTER`` is imported rather than restated: the lens that asks "what
# was left behind?" and the rule that claims it must not be able to disagree
# about where that line falls.

# How many sweep intervals a whole kind may go unobserved before the fault is
# the sweep rather than any one record. Three, because one missed tick is a
# restart and two is a slow provider.
_KIND_SILENT_AFTER = 3
_CLAIM_NAMESPACE = "infrastructure.finding"


@dataclass(frozen=True)
class Remedy:
    """An existing capability, named -- never a new way to change anything."""

    capability: str
    target: str
    label: str
    effect: str
    url: str = ""
    method: str = "GET"
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
    # Safe, already-authorized read workflows emitted by the subject node.
    # Delivery adapters render these; they do not rediscover or filter them.
    offers: tuple[ActionLink, ...] = ()
    # Canonical graph investigations derived once from the finding subject.
    investigations: tuple[ActionLink, ...] = ()
    # A kind rather than a node, for a claim about a whole class. The estate
    # keeps its honesty: no synthetic node is invented to hang this on.
    scope: str = ""
    # Kinds this higher-order claim explains. They remain exact machine facts,
    # while an effortless surface can lead with the shared cause once.
    affected_scopes: tuple[str, ...] = ()
    workflow: WorkflowPlan | None = None


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
    controllers_by_kind: dict[str, frozenset[str]]

    def nodes(self) -> tuple[TopologyNode, ...]:
        return self.topology.nodes


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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
    by_id = {node.id: node for node in topology.nodes}
    return _Estate(
        topology,
        timezone.now(),
        latest,
        observed,
        governed,
        frozenset(counts),
        counts,
        _controllers_by_kind(topology, by_id),
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


def _controller_sweep_stale(estate: _Estate) -> tuple[Finding, ...]:
    """Several stale kinds sharing one controller are one upstream failure.

    Kind-level findings stay useful evidence for machines. An operator should
    not have to correlate them by timestamp and provider, though: topology
    already says which controller carries the connections that enable each
    kind. When at least two stale kinds converge there, HQ can name the cause,
    trace its impact, and offer the controller's existing safe read actions.
    """

    stale_kinds = {
        finding.scope for finding in _kind_never_swept(estate) if finding.scope
    }
    grouped: dict[str, set[str]] = {}
    for kind in stale_kinds:
        for controller in estate.controllers_by_kind.get(kind, frozenset()):
            grouped.setdefault(controller, set()).add(kind)
    by_id = {node.id: node for node in estate.nodes()}
    return tuple(
        Finding(
            rule="controller-sweep-stale",
            subject=controller,
            title=f"{by_id[controller].label} stopped confirming {len(kinds)} kinds",
            severity="serious",
            explanation=(
                "These kinds became stale behind connections carried by the same "
                "controller. HQ has correlated the downstream symptoms into one "
                "upstream failure; inspect that connection once, then trace every "
                "affected declaration from here."
            ),
            evidence=(
                ("Affected kinds", ", ".join(sorted(kinds))),
                ("Shared cause", by_id[controller].label),
                (
                    "What HQ can do",
                    "wake its controller, open its connections, and trace the affected estate",
                ),
            ),
            remedies=(
                Remedy(
                    "infrastructure.controller.refresh",
                    "",
                    "Request fresh sweep",
                    "",
                ),
            ),
            affected_scopes=tuple(sorted(kinds)),
        )
        for controller, kinds in sorted(grouped.items())
        if len(kinds) >= 2 and controller in by_id
    )


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
            title=(
                f"{node.label} asserts {len(node.unconfirmed_fields)} "
                f"unconfirmed field"
                f"{'s' if len(node.unconfirmed_fields) != 1 else ''}"
            ),
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


def _reached_but_unmeasured(estate: _Estate) -> tuple[Finding, ...]:
    """A name a connection reports reaching that nothing is measuring.

    The first claim here that neither half of HQ can make alone. Infrastructure
    knows a connection reaches this name; analytics knows what every name it
    watches served. Put beside each other they answer a question neither was
    asked: which of the things we run is nobody watching.

    ``None`` and zero are the whole rule. A measured site with no visitors is a
    fact about the site; an unmeasured one is a fact about HQ, and only the
    second is a gap someone can close.

    Restricted to observed targets on purpose. A declaration is a statement of
    intent and may name something not serving anything yet, but a target is a
    name a live connection said it *reaches* -- so it is answering, and nothing
    is counting.

    Gated on a measured sibling, the same way a skipped record is judged against
    the sweep that confirmed its siblings. Most things HQ reaches are containers
    and proxy entries that will never carry a web beacon, and saying so about
    each would bury the queue in claims nobody can act on. But a connection with
    four measured names and a fifth without one is a gap someone can close, and
    that is the only shape this fires on.
    """

    measured_peers: dict[str, bool] = {}
    reached_by: dict[str, str] = {}
    for edge in estate.topology.edges:
        if edge.kind != "reaches":
            continue
        reached_by.setdefault(edge.target, edge.source)
    by_id = {node.id: node for node in estate.nodes()}
    for target_id, connection_id in reached_by.items():
        node = by_id.get(target_id)
        if node is not None and node.pageviews is not None:
            measured_peers[connection_id] = True

    return tuple(
        Finding(
            rule="reached-but-unmeasured",
            subject=node.id,
            title=f"{node.label} is reachable and unmeasured",
            severity="attention",
            explanation=(
                "A live connection reports reaching this name, and other names "
                "on the same connection do report traffic. Nothing here says the "
                "site is idle -- it says nobody is counting, so a drop in use "
                "would look exactly like a steady one."
            ),
            evidence=(
                (
                    "Reached by",
                    (
                        by_id.get(reached_by[node.id]).label
                        if by_id.get(reached_by[node.id])
                        else "a connection"
                    ),
                ),
                ("Measured", "nothing reports traffic for this name"),
            ),
        )
        for node in estate.nodes()
        if node.kind == "target"
        and node.pageviews is None
        and node.id in reached_by
        and measured_peers.get(reached_by[node.id])
    )


def _registration_lapsing(estate: _Estate) -> tuple[Finding, ...]:
    """A domain that runs out and will not renew itself.

    The one fact about a domain no other credential here can see, and the only
    one that takes everything else with it. HQ renews the certificate,
    reconciles the records and serves every name inside the zone -- and none of
    it survives the registration lapsing. Cloudflare will serve that zone
    perfectly for a domain about to stop being yours.

    Both halves are the rule. An expiry alone fires on every domain every year
    and is a calendar, not a finding; an expiry with auto-renew off is an outage
    with a countdown. The registrar knows the second, which is why the sweep
    reads the registrar rather than RDAP -- RDAP is public and free and can only
    ever answer the half that means nothing on its own.

    Ninety days, matching the window a certificate gets: long enough to act on a
    domain whose renewal failed, short enough not to live in the queue.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        facts = dict(node.facts)
        expires = _parse(facts.get("expires_at", ""))
        if expires is None or facts.get("auto_renew") != "no":
            continue
        # A registrar reports a date, and a date parses naive. Compared against
        # an aware now that raises rather than answering, so the assumption is
        # made explicit here: a renewal date is a UTC day.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt_timezone.utc)
        days = (expires - estate.now).days
        if days > 90:
            continue
        domain = facts.get("domain", node.label)
        found.append(
            Finding(
                rule="registration-lapsing",
                subject=node.id,
                title=f"{domain} expires in {days} days and will not renew",
                severity="serious" if days <= 30 else "attention",
                explanation=(
                    f"{domain} runs out on {expires.date().isoformat()} and "
                    "auto-renew is off at the registrar. Everything HQ does for "
                    "this domain -- the records, the certificate, every name "
                    "served inside it -- stops the day it lapses, and nothing "
                    "else here would notice."
                ),
                evidence=(
                    ("Expires", expires.date().isoformat()),
                    ("Auto-renew", "off"),
                    ("Registrar", facts.get("registrar", "unknown")),
                ),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "registration-lapsing",
        "A domain registration is running out",
        "serious",
        _registration_lapsing,
    ),
    FindingRule(
        "controller-sweep-stale",
        "A controller stopped confirming its estate",
        "serious",
        _controller_sweep_stale,
        subsumes=("kind-never-swept",),
    ),
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
    FindingRule(
        "reached-but-unmeasured",
        "Reachable and unmeasured",
        "attention",
        _reached_but_unmeasured,
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


def _resolved(
    finding: Finding, principal: Principal, subject: TopologyNode | None
) -> Finding:
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
    findings_url = route_url("control_plane:findings")
    verification = (
        ActionLink(
            "verify",
            "Recheck from current facts",
            "read",
            f"{findings_url}?{urlencode({'rule': finding.rule})}",
            reason=(
                "HQ re-derives the same rule from the newest authorized topology."
            ),
        )
        if findings_url
        else None
    )
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


def _serialize(finding: Finding) -> dict[str, Any]:
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
