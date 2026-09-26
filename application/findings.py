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
from datetime import datetime, timezone as dt_timezone
from ipaddress import ip_network
from typing import Any, Callable
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone

from control_plane.providers import CONTAINER_KIND, PROVIDERS

from .action_links import ActionLink, action_with_return, topology_investigation_links
from .integrations import IntegrationGraph, integration_graph
from .reach import TAILNET
from .cadence import slowest_sweep_interval as _slowest_sweep_interval, sweep_interval
from .contracts import route_url
from .security import AuthorizationError, Principal
from .topology import (
    _STALE_AFTER,
    JOINED_KINDS,
    Topology,
    TopologyNode,
    derive_topology,
)
from .ui import counted, duration
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
#
# Multiplied against the *slowest* interval HQ is willing to sweep at, not the
# one currently in force. Reading the live value made this threshold swing with
# the thing it watches (minutes while somebody was on the page, half a day
# once nobody was) so it was by turns too tight to trust and too loose to
# help. A fixed ceiling is at least a number that can be reasoned about.
#
# It is still a statement about sweeps, not about liveness: nothing here can
# notice a controller that died between two scheduled sweeps, because the only
# evidence it reads is when records were last confirmed. Detecting that needs
# the controller to check in on its own clock.
_KIND_SILENT_AFTER = 3
_CLAIM_NAMESPACE = "infrastructure.finding"


@dataclass(frozen=True)
class Remedy:
    """An existing capability, named: never a new way to change anything."""

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


def _open_the_path(node: TopologyNode) -> tuple[Remedy, ...]:
    """Amend the policy, and look again once it is amended.

    The capability is handed the resource, not a path: which one to open is
    re-derived from what was observed, so a remedy can never carry an access
    rule of its own. The amendment lands on a gated kind, so a person still
    consents before the tailnet changes.
    """

    return (
        Remedy(
            capability="tailnet.reach.allow",
            target=node.label,
            label="Allow the path",
            effect="infrastructure_change",
        ),
        *_reconcile(node),
    )


def _reconcile(node: TopologyNode) -> tuple[Remedy, ...]:
    """The one capability that answers "go and look again", where the kind allows it."""

    provider = PROVIDERS.get(node.kind_key)
    policy = provider.actions.get("reconcile") if provider else None
    if policy is not None and policy.mode == "locked":
        return ()
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
    so siblings land together. One left behind was not slow, it was skipped,
    and being skipped is invisible in every other surface, because the thing
    keeps whatever it last said about itself.
    """

    found = []
    for node in estate.nodes():
        moment = estate.observed.get(node.id)
        newest = estate.latest_by_kind.get(node.kind_key)
        if moment is None or newest is None or not node.managed or node.on_demand:
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
                title=f"{node.label} was not in the last sweep",
                severity="serious",
                explanation=(
                    f"Last seen {duration(behind)} before "
                    + (
                        f"the one other {node.subtitle.lower()} record. "
                        if siblings == 2
                        else f"the other {siblings - 1} {node.subtitle.lower()} records. "
                    )
                    + (
                        "Mark it on demand if it only runs sometimes, or remove it."
                        if node.kind_key == CONTAINER_KIND
                        else "Reconcile it, or remove it if it is gone."
                    )
                ),
                evidence=(
                    ("Last seen", node.observed_at),
                    ("Newest of this kind", newest.isoformat()),
                    ("Behind by", duration(behind)),
                    ("Records of this kind seen", str(siblings)),
                    ("Reason", node.reason or "none"),
                ),
                remedies=_skipped_remedies(node),
            )
        )
    return tuple(found)


def _unrecognised_containers(estate: _Estate) -> tuple[Finding, ...]:
    """A container a sweep found that no compose project declares.

    Every container that belongs on a machine is created by a compose project,
    and HQ takes those on by itself. Anything else was started by hand or by
    something HQ does not know, so it is reported rather than adopted: a
    container nobody declared must never look like one somebody did.
    """

    from django.urls import NoReverseMatch, reverse

    from .inventory import record_token

    found: list[Finding] = []
    for node in estate.nodes():
        for key, value in node.facts:
            if key != "unrecognised-container":
                continue
            name, _, host = value.rpartition("@")
            try:
                adopt_url = reverse(
                    "control_plane:adopt_record",
                    args=[
                        CONTAINER_KIND,
                        record_token(CONTAINER_KIND, (host, name)),
                    ],
                )
            except NoReverseMatch:
                adopt_url = ""
            found.append(
                Finding(
                    rule="unrecognised-container",
                    subject=node.id,
                    title=f"Unrecognised container {name} on {node.label}",
                    severity="serious",
                    explanation=(
                        "No compose project started it, so HQ has not taken it on. "
                        "Adopt it if you started it. Otherwise find what did and remove it."
                    ),
                    evidence=(("Container", name), ("Machine", node.label)),
                    remedies=(
                        Remedy(
                            capability="infrastructure.resource.create",
                            target=name,
                            label="Adopt it",
                            effect="HQ starts watching it like any other container.",
                            url=adopt_url,
                            method="POST",
                        ),
                    ),
                )
            )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _skipped_remedies(node: TopologyNode) -> tuple[Remedy, ...]:
    """The two answers the explanation gives, as the actions that carry them out.

    A container missing from a sweep is either one that runs now and then, or
    one that is gone. Reconciling answers neither: it asks the provider to look
    again at something that is, correctly, not running.
    """

    remove = Remedy(
        capability="infrastructure.resource.remove",
        target=node.label,
        label="Review removal",
        effect="",
    )
    if node.kind_key == CONTAINER_KIND:
        return (
            Remedy(
                capability="infrastructure.resource.update",
                target=node.label,
                label="Mark on demand",
                effect="",
            ),
            remove,
        )
    return (*_reconcile(node), remove)


def _is_observable(kind_key: str) -> bool:
    """Whether anything can ever produce an observation of this kind.

    A printer, a network, an offline CA, a certificate delivery target: HQ was
    told about them, nothing adopts them from a sweep and no controller acts on
    them. Judging one by when it was last observed asks for a reading that
    cannot exist, so the claim it raises can never be cleared.

    This rule measures against the sweep interval, so it only means anything
    for a kind a sweep visits. A certificate is observed when an operation
    issues or installs it, which is nothing like every sixty seconds: judged
    on that cadence it is permanently overdue and no sweep or reconcile can
    ever settle it. Its real risk, expiry, is reported where it belongs.

    `unobserved_reason` is the provider's own statement that no collector
    sweeps it, and the collector registry is joined to it by test, so this is
    read rather than derived a second time.
    """

    provider = PROVIDERS.get(kind_key)
    return not (provider and provider.unobserved_reason)


def _kind_never_swept(estate: _Estate) -> tuple[Finding, ...]:
    """A whole kind that nothing has observed lately.

    The sibling comparison above cannot see this: a kind with one record has
    nothing to be behind, and a kind where every record is equally stale looks
    perfectly consistent. Ship the two together or the hole is still open,
    and this one is deliberately blunt, an absolute clock against the interval
    HQ itself declares, because that is the only thing left to compare against.
    """

    interval = _slowest_sweep_interval() * _KIND_SILENT_AFTER
    found = []
    for kind_key, newest in sorted(estate.latest_by_kind.items()):
        silent = estate.now - newest
        if silent <= interval or not _is_observable(kind_key):
            continue
        # Staleness is a statement about declarations, and a kind HQ declares
        # nothing of has none to be stale. An inventory also carries rows
        # written on their own schedule by something that is not the sweep;
        # against the sweep interval those are overdue on every read.
        if kind_key not in estate.declared_kinds:
            continue
        found.append(
            Finding(
                rule="kind-never-swept",
                subject="",
                scope=kind_key,
                title=f"No {kind_key} record seen for {duration(silent)}",
                severity="serious",
                explanation=(
                    f"Every record of this kind is at least {duration(silent)} old, "
                    "so the sweep is not reaching it. Request a fresh sweep, and "
                    "check its connection if nothing changes."
                ),
                evidence=(
                    ("Last seen", newest.isoformat()),
                    ("Not seen for", duration(silent)),
                    ("Sweep interval", duration(sweep_interval())),
                ),
                remedies=(
                    Remedy(
                        capability="infrastructure.controller.refresh",
                        target="",
                        label="Request fresh sweep",
                        effect="",
                    ),
                ),
            )
        )
    # A kind with no observation at all has no newest to be behind, so the loop
    # above cannot see it, and left alone it becomes one finding per record.
    # Against a real estate that was three hundred and twenty claims saying the
    # same thing once each, which is how a queue stops being read. Said once
    # about the kind, it is one line and the same information.
    for kind_key in sorted(estate.declared_kinds - set(estate.latest_by_kind)):
        if not _is_observable(kind_key):
            continue
        found.append(
            Finding(
                rule="kind-never-swept",
                subject="",
                scope=kind_key,
                title=f"No {kind_key} record has ever been seen",
                severity="serious",
                explanation=(
                    "The sweep has never reached this kind, so its records show "
                    "only what was declared. Request a fresh sweep, and check its "
                    "connection if nothing changes."
                ),
                evidence=(
                    ("Last seen", "never"),
                    (
                        "Records of this kind",
                        str(estate.declared_counts.get(kind_key, 0)),
                    ),
                    ("Sweep interval", duration(sweep_interval())),
                ),
                remedies=(
                    Remedy(
                        capability="infrastructure.controller.refresh",
                        target="",
                        label="Request fresh sweep",
                        effect="",
                    ),
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
            title=f"{by_id[controller].label} stopped reporting {len(kinds)} kinds",
            severity="serious",
            explanation=(
                "Every stale kind goes through this controller. Check the "
                "controller and its connections first."
            ),
            evidence=(
                ("Affected kinds", ", ".join(sorted(kinds))),
                ("Controller", by_id[controller].label),
                ("Next", "check its connections"),
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


def _reporting_a_fault(estate: _Estate) -> tuple[Finding, ...]:
    """A resource whose own condition says it is wrong, now.

    `reconciled-but-still-wrong` covers the case where a reconcile has already
    been tried against this exact declaration. This is the rest: a fault the
    resource is reporting while a change is still outstanding, which nothing
    else here was watching. A certificate marked expiring reached the queue
    only through an unrelated staleness rule, so it left with it.
    """

    return tuple(
        Finding(
            rule="reporting-a-fault",
            subject=node.id,
            title=f"{node.label} reports {node.status_label or node.status}",
            severity="serious" if node.status == "serious" else "attention",
            explanation=(
                "The resource reports this itself, and a declared change is not "
                "applied yet. The detail is the provider's message."
            ),
            evidence=(
                ("Status", node.status_label or node.status),
                ("Reason", node.reason or "none"),
                ("Detail", node.detail or "none"),
                ("Declared revision", str(node.declared_revision)),
                ("Observed revision", str(node.observed_revision)),
            ),
            remedies=_reconcile(node),
        )
        for node in estate.nodes()
        if node.kind == "resource"
        and node.managed
        and node.status in {"attention", "serious"}
        # The other rule owns the converged case and says something sharper
        # about it, so this one takes what is left rather than doubling it.
        and node.declared_revision != node.observed_revision
    )


def _reconciled_but_still_wrong(estate: _Estate) -> tuple[Finding, ...]:
    """Converged on paper, disagreeing in practice.

    The two revisions match, so everything that asks "has anything changed?"
    answers no and nothing is queued, while the status says otherwise. That
    combination means the reconcile already ran against this exact declaration
    and the world still disagrees, so running it again will not help. This is
    the one shape where the declaration itself is the suspect.
    """

    return tuple(
        Finding(
            rule="reconciled-but-still-wrong",
            subject=node.id,
            title=f"{node.label} is still wrong after reconciling",
            severity="serious",
            explanation=(
                "Declared and observed revisions match and the status is still "
                f"{node.status_label or node.status}. HQ will not retry. "
                "Check the declaration."
            ),
            evidence=(
                ("Status", node.status_label or node.status),
                ("Declared revision", str(node.declared_revision)),
                ("Observed revision", str(node.observed_revision)),
                ("Reason", node.reason or "none"),
                ("Detail", node.detail or "none"),
            ),
            # Reconciling again is the one thing already known not to work, so
            # the remedy is the declaration this rule points at.
            remedies=(
                Remedy(
                    capability="infrastructure.resource.update",
                    target=node.label,
                    label="Edit declaration",
                    effect="",
                ),
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
    nothing governs is uncovered rather than skipped: a different finding with
    a different answer. And an observed sibling, because without one the whole
    kind is unreached and that belongs to ``kind-never-swept``, said once.
    """

    return tuple(
        Finding(
            rule="never-observed",
            subject=node.id,
            title=f"{node.label} has never been seen",
            severity="attention",
            explanation=(
                "Other records of this kind have been seen, this one never. "
                "HQ shows only what was declared."
            ),
            evidence=(
                ("Last seen", "never"),
                ("Declared revision", str(node.declared_revision)),
                ("Observed revision", str(node.observed_revision)),
            ),
            remedies=_reconcile(node),
        )
        for node in estate.nodes()
        if node.kind == "resource"
        and node.managed
        and not node.observed_at
        and _is_observable(node.kind_key)
        and node.id in estate.governed
        # A sibling has been observed, so the sweep can see this kind and
        # missed this one. Without that, the gap is the kind's, and saying it
        # per record buries the one claim worth reading.
        and node.kind_key in estate.latest_by_kind
    )


def _weakly_verified(estate: _Estate) -> tuple[Finding, ...]:
    """Observed, and still asserting things the observation never confirmed.

    Drift is judged only across fields both sides carry, so a field the reading
    omits is not agreed: it is unjudged. A record can therefore be confirmed,
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
                f"{node.label} has "
                f"{counted(len(node.unconfirmed_fields), 'unconfirmed field', 'unconfirmed fields')}"
            ),
            severity="attention",
            explanation=(
                "The last sweep confirmed this record but not these fields, so "
                "their values are unchecked. Make the provider report them, or "
                "declare that it cannot."
            ),
            evidence=(
                ("Unconfirmed", ", ".join(node.unconfirmed_fields)),
                ("Last seen", node.observed_at),
                ("Reason", node.reason or "none"),
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
    name a live connection said it *reaches*, so it is answering, and nothing
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
            title=f"{node.label} has no traffic measurement",
            severity="attention",
            explanation=(
                "Other names on the same connection report traffic. This one "
                "does not. Add it to analytics."
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
                ("Traffic", "not measured"),
            ),
        )
        for node in estate.nodes()
        if node.kind in _REACHED_KINDS
        and node.pageviews is None
        and node.id in reached_by
        and measured_peers.get(reached_by[node.id])
    )


# Node kinds a connection reaches by name: a target, or the estate node it folded into.
_REACHED_KINDS = frozenset({"target", "service", "zone"})


def _registration_lapsing(estate: _Estate) -> tuple[Finding, ...]:
    """A domain that runs out and will not renew itself.

    The one fact about a domain no other credential here can see, and the only
    one that takes everything else with it. HQ renews the certificate,
    reconciles the records and serves every name inside the zone, and none of
    it survives the registration lapsing. Cloudflare will serve that zone
    perfectly for a domain about to stop being yours.

    Both halves are the rule. An expiry alone fires on every domain every year
    and is a calendar, not a finding; an expiry with auto-renew off is an outage
    with a countdown. The registrar knows the second, which is why the sweep
    reads the registrar rather than RDAP: RDAP is public and free and can only
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
                    f"On {expires.date().isoformat()} its "
                    "records, certificate and every name under it stop working. "
                    "Renew it or turn on auto-renew at the registrar."
                ),
                evidence=(
                    ("Expires", expires.date().isoformat()),
                    ("Auto-renew", "off"),
                    ("Registrar", facts.get("registrar", "unknown")),
                ),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _fact_values(node, key: str) -> tuple[str, ...]:
    """The non-empty values a node carries under one fact key."""

    return tuple(value for fact, value in node.facts if fact == key and value)


def _perimeter_open(estate: _Estate) -> tuple[Finding, ...]:
    """A port answering from the public internet that nothing means to publish.

    Not a disagreement about configuration. The port was dialled from outside
    and it answered, so this is the invariant already broken rather than a
    prediction that it might be.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        ports = _fact_values(node, "answers-publicly")
        if not ports:
            continue
        found.append(
            Finding(
                rule="perimeter-open",
                subject=node.id,
                title=(
                    f"{node.label} answers on "
                    f"{', '.join(ports)} from the public internet"
                ),
                severity="serious",
                explanation=(
                    "A probe from outside the tailnet got an answer, so anything "
                    "behind these ports is public. Close them at the firewall."
                ),
                evidence=tuple(("Answers publicly", port) for port in ports),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _firewall_stopped(estate: _Estate) -> tuple[Finding, ...]:
    """A firewall that is installed, configured, and not running.

    Enabled and dead is the state with no symptom: the rules are on disk, the
    unit is listed, and nothing is filtering. It survives a reboot as silence,
    which is the one time it is most likely to happen.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        state = next(
            (value for key, value in node.facts if key == "firewall-unit" and value),
            "",
        )
        if not state:
            continue
        found.append(
            Finding(
                rule="firewall-stopped",
                subject=node.id,
                title=f"{node.label} is not running its firewall",
                severity="serious",
                explanation=(
                    f"The firewall unit is {state}, so its rules are not "
                    "applied. Start the unit."
                ),
                evidence=(("Firewall unit", state),),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _connection_not_answering(estate: _Estate) -> tuple[Finding, ...]:
    """A connection whose last probe got no answer, or whose credential is refused."""

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        refusal = next(
            (value for key, value in node.facts if key == "credential-refused"), None
        )
        refused = refusal is not None
        if node.status != "serious" and not refused:
            continue
        reason = refusal or node.detail
        found.append(
            Finding(
                rule="connection-not-answering",
                subject=node.id,
                title=(
                    f"{node.label}'s credential is refused"
                    if refused
                    else f"{node.label} is not answering"
                ),
                severity="attention",
                explanation=(
                    (f"{reason.rstrip('.')}. " if reason else "")
                    + (
                        "The provider refuses the credential, so HQ reads nothing "
                        "through it until it is replaced."
                        if refused
                        else "HQ reads nothing through it until it answers, so what "
                        "it reaches may be out of date."
                    )
                ),
                evidence=(
                    ("State", "Refused" if refused else node.status_label or "Unreachable"),
                    *((("Last observed", node.observed_at),) if node.observed_at else ()),
                ),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _devices_join_without_approval(estate: _Estate) -> tuple[Finding, ...]:
    """Device approval is off: any valid auth key adds a device with no review."""

    return tuple(
        Finding(
            rule="devices-join-without-approval",
            subject=node.id,
            title="New devices join the tailnet without approval",
            severity="attention",
            explanation=(
                "Any valid auth key adds a device with no review. Turn on device "
                "approval in the tailnet settings."
            ),
            evidence=(("Device approval", "Off"),),
        )
        for node in estate.nodes()
        if node.kind == "connection"
        and any(key == "devices-join-unapproved" for key, _ in node.facts)
    )


def _empty_group_granted(estate: _Estate) -> tuple[Finding, ...]:
    """A group with no members that a grant or shell rule still names."""

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        empty = _fact_values(node, "empty-group-granted")
        if not empty:
            continue
        found.append(
            Finding(
                rule="empty-group-granted",
                subject=node.id,
                title=(
                    f"{empty[0]} has no members but is still granted access"
                    if len(empty) == 1
                    else f"{counted(len(empty), 'group has', 'groups have')} no "
                    "members but are still granted access"
                ),
                severity="neutral",
                explanation=(
                    "A grant naming an empty group admits nobody. Remove the group "
                    "from the policy, or add the members it was meant for."
                ),
                evidence=tuple(("Empty group", name) for name in empty),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _work_that_keeps_failing(estate: _Estate) -> tuple[Finding, ...]:
    """A connection HQ can open and cannot use.

    A probe asks whether the credential still works. It is answered by the
    door, not by the room: a connection can pass every probe while every piece
    of work sent through it refuses, and the page will say "reachable" the
    whole time.

    The failures are caught by the code that sends the work, so they never
    become an operation anybody sees. They repeat on the next pass, at whatever
    interval the controller runs, for as long as nobody looks at a log.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        unfinished = _fact_values(node, "work-unfinished")
        if not unfinished:
            continue
        found.append(
            Finding(
                rule="work-that-keeps-failing",
                subject=node.id,
                title=(
                    f"{node.label} is reachable, but "
                    f"{counted(len(unfinished), 'task through it', 'tasks through it')} "
                    "did not finish"
                ),
                severity="attention",
                explanation=(
                    "The credential works, but the last pass could not finish "
                    "this work. It retries every pass and never shows up as an "
                    "operation. Check the controller log for the cause."
                ),
                evidence=tuple(("Could not finish", item) for item in unfinished),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _unreachable_consumer(estate: _Estate) -> tuple[Finding, ...]:
    """A name this estate serves that the last reading could not reach.

    Not a claim that the certificate is wrong: the other consumers were read
    and agree. It is a claim about one path, from the machine that verifies to
    the machine that serves, and a path is the kind of thing a network policy
    decides rather than a certificate. Left unread, a consumer keeps whatever
    it was last given and nothing here would ever notice it going stale, which
    is the failure this rule exists to make loud.

    Read from the node's facts rather than a dictionary of them: every entry
    here shares one key, and collapsing them would report one unreachable name
    however many there were.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        names = _fact_values(node, "unreachable")
        if not names:
            continue
        # The tailnet refusing the path is a different claim from the consumer
        # being down, and only one of them has a fix worth offering. Present,
        # the policy was asked and said no; absent, it said yes or was never
        # swept, and neither is a reason to send anybody at an access policy.
        refused = _fact_values(node, "path-denied")
        found.append(
            Finding(
                rule="unreachable-consumer",
                subject=node.id,
                title=(
                    f"{node.label} could not read {names[0]}"
                    if len(names) == 1
                    else f"{node.label} could not read {len(names)} of its consumers"
                ),
                severity="serious",
                explanation=(
                    "The last reading reached every other consumer. This one "
                    "did not answer, so HQ cannot tell which certificate it "
                    "serves."
                    + (
                        " The tailnet policy blocks the path. Allow it, then "
                        "reconcile."
                        if refused
                        else ""
                    )
                ),
                evidence=(
                    *(("Not read", name) for name in names),
                    *(("Blocked by tailnet policy", path) for path in refused),
                ),
                remedies=_open_the_path(node) if refused else _reconcile(node),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _tailnet_dns_off_tailnet(estate: _Estate) -> tuple[Finding, ...]:
    """A tailnet resolver that is not the address of any tailnet device.

    Clients then reach it through a subnet router or the internet, and it sees
    one source address for all of them rather than each device.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        resolvers = tuple(
            value
            for key, value in node.facts
            if key == "tailnet-dns-off-tailnet" and value
        )
        if not resolvers:
            continue
        found.append(
            Finding(
                rule="tailnet-dns-off-tailnet",
                subject=node.id,
                title=(
                    f"{counted(len(resolvers), 'tailnet nameserver is', 'tailnet nameservers are')} "
                    f"off the tailnet: {', '.join(resolvers)}"
                ),
                severity="attention",
                explanation=(
                    "No tailnet device has this address, so clients query it "
                    "through a subnet router or the internet and it sees one "
                    "source address instead of each device. Set the tailnet "
                    "nameserver to the DNS server's tailnet address."
                ),
                evidence=tuple(("Nameserver", resolver) for resolver in resolvers),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


_TAILNET_RANGE = TAILNET[0]


def _trusted_wider_than_tailnet(estate: _Estate) -> tuple[Finding, ...]:
    """Trusted networks admit the whole Tailscale IPv4 range; the tailnet uses less.

    Informational. Trust is configuration, and HQ never narrows it itself.
    """

    wide = []
    for cidr in settings.SEVERINO_TRUSTED_NETWORKS:
        try:
            network = ip_network(str(cidr).strip(), strict=False)
        except ValueError:
            continue
        if network.version == 4 and network.supernet_of(_TAILNET_RANGE):
            wide.append(str(network))
    if not wide:
        return ()
    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        addresses = tuple(v for k, v in node.facts if k == "tailnet-address" and v)
        routes = tuple(v for k, v in node.facts if k == "tailnet-route" and v)
        if not addresses:
            continue
        uses = counted(len(addresses), "device address", "device addresses")
        if routes:
            uses += f" and {counted(len(routes), 'subnet route', 'subnet routes')}"
        found.append(
            Finding(
                rule="trusted-wider-than-tailnet",
                subject=node.id,
                title=f"HQ trusts all of {_TAILNET_RANGE}; the tailnet uses {uses}",
                severity="neutral",
                explanation=(
                    "SEVERINO_TRUSTED_NETWORKS admits every address in the range. "
                    "Narrowing it to what the tailnet uses is an operator's "
                    "decision; HQ does not change it."
                ),
                evidence=(
                    *(("Trusted", network) for network in wide),
                    *(("Device address", address) for address in addresses),
                    *(("Subnet route", route) for route in routes),
                ),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "unrecognised-container",
        "A container no compose project declares",
        "serious",
        _unrecognised_containers,
    ),
    FindingRule(
        "perimeter-open",
        "Port open to the public internet",
        "serious",
        _perimeter_open,
    ),
    FindingRule(
        "firewall-stopped",
        "Firewall not running",
        "serious",
        _firewall_stopped,
    ),
    FindingRule(
        "connection-not-answering",
        "Connection not answering or refused",
        "attention",
        _connection_not_answering,
    ),
    FindingRule(
        "work-that-keeps-failing",
        "Work through a connection fails",
        "attention",
        _work_that_keeps_failing,
    ),
    FindingRule(
        "unreachable-consumer",
        "Consumer could not be read",
        "serious",
        _unreachable_consumer,
        # Says the same thing with the name of the consumer in it. The generic
        # rule would otherwise put this in front of an operator twice.
        subsumes=("reporting-a-fault",),
    ),
    FindingRule(
        "registration-lapsing",
        "Domain registration expiring",
        "serious",
        _registration_lapsing,
    ),
    FindingRule(
        "controller-sweep-stale",
        "Controller stopped reporting",
        "serious",
        _controller_sweep_stale,
        subsumes=("kind-never-swept",),
    ),
    FindingRule(
        "skipped-by-a-sweep",
        "Missing from the last sweep",
        "serious",
        _skipped_by_a_sweep,
    ),
    FindingRule(
        "kind-never-swept",
        "Kind not seen by the sweep",
        "serious",
        _kind_never_swept,
        # When the sweep itself is the fault, every record of the kind looks
        # skipped. Saying it once about the kind beats saying it about each.
        subsumes=("skipped-by-a-sweep", "never-observed"),
    ),
    FindingRule(
        "reporting-a-fault",
        "Resource reports a fault",
        "serious",
        _reporting_a_fault,
    ),
    FindingRule(
        "reconciled-but-still-wrong",
        "Still wrong after reconciling",
        "serious",
        _reconciled_but_still_wrong,
    ),
    FindingRule(
        "weakly-verified",
        "Unconfirmed fields",
        "attention",
        _weakly_verified,
    ),
    FindingRule(
        "never-observed",
        "Never seen",
        "attention",
        _never_observed,
    ),
    FindingRule(
        "reached-but-unmeasured",
        "Traffic not measured",
        "attention",
        _reached_but_unmeasured,
    ),
    FindingRule(
        "tailnet-dns-off-tailnet",
        "Tailnet DNS is not a tailnet address",
        "attention",
        _tailnet_dns_off_tailnet,
    ),
    FindingRule(
        "devices-join-without-approval",
        "New devices join without approval",
        "attention",
        _devices_join_without_approval,
    ),
    FindingRule(
        "empty-group-granted",
        "Empty group still granted access",
        "neutral",
        _empty_group_granted,
    ),
    FindingRule(
        "trusted-wider-than-tailnet",
        "Trusted networks wider than the tailnet",
        "neutral",
        _trusted_wider_than_tailnet,
    ),
)

_RULE_BY_NAME = {rule.name: rule for rule in RULES}


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
    findings_url = route_url("control_plane:findings")
    verification = (
        ActionLink(
            "verify",
            "Check again",
            "read",
            f"{findings_url}?{urlencode({'rule': finding.rule})}",
            reason="Runs this check against current data.",
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
