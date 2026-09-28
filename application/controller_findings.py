"""Findings about the controller's own work: kinds it stopped sweeping, records it skipped or never saw, and changes it keeps applying without effect."""

from __future__ import annotations

from control_plane.provider_adapters.portainer import CONTAINER_KIND

from .cadence import slowest_sweep_interval as _slowest_sweep_interval, sweep_interval
from .topology import (
    _STALE_AFTER,
    TopologyNode,
)
from .ui import counted, duration
from .finding_model import Finding, Remedy, FindingEstate, fact_values, is_observable, reconcile_remedy
from .finding_model import FindingRule


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


def _skipped_by_a_sweep(estate: FindingEstate) -> tuple[Finding, ...]:
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
    return (*reconcile_remedy(node), remove)


def _kind_never_swept(estate: FindingEstate) -> tuple[Finding, ...]:
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
        if silent <= interval or not is_observable(kind_key):
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
        if not is_observable(kind_key):
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


def _controller_sweep_stale(estate: FindingEstate) -> tuple[Finding, ...]:
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


def _reporting_a_fault(estate: FindingEstate) -> tuple[Finding, ...]:
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
            remedies=reconcile_remedy(node),
        )
        for node in estate.nodes()
        if node.kind == "resource"
        and node.managed
        and node.status in {"attention", "serious"}
        # The other rule owns the converged case and says something sharper
        # about it, so this one takes what is left rather than doubling it.
        and node.declared_revision != node.observed_revision
    )


def _reconciled_but_still_wrong(estate: FindingEstate) -> tuple[Finding, ...]:
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


def _never_observed(estate: FindingEstate) -> tuple[Finding, ...]:
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
            remedies=reconcile_remedy(node),
        )
        for node in estate.nodes()
        if node.kind == "resource"
        and node.managed
        and not node.observed_at
        and is_observable(node.kind_key)
        and node.id in estate.governed
        # A sibling has been observed, so the sweep can see this kind and
        # missed this one. Without that, the gap is the kind's, and saying it
        # per record buries the one claim worth reading.
        and node.kind_key in estate.latest_by_kind
    )


def _weakly_verified(estate: FindingEstate) -> tuple[Finding, ...]:
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
            remedies=reconcile_remedy(node),
        )
        for node in estate.nodes()
        if node.kind == "resource" and node.managed and node.unconfirmed_fields
    )


def _work_that_keeps_failing(estate: FindingEstate) -> tuple[Finding, ...]:
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
        unfinished = fact_values(node, "work-unfinished")
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


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "work-that-keeps-failing",
        "Work through a connection fails",
        "attention",
        _work_that_keeps_failing,
        operator_action=(
            "Read the controller log for the failing step on this connection and fix what it names."
        ),
    ),
    FindingRule(
        "controller-sweep-stale",
        "Controller stopped reporting",
        "serious",
        _controller_sweep_stale,
        operator_action=(
            "Check that the controller runs and can reach HQ, then request a fresh sweep."
        ),
        subsumes=("kind-never-swept",),
    ),
    FindingRule(
        "skipped-by-a-sweep",
        "Missing from the last sweep",
        "serious",
        _skipped_by_a_sweep,
        operator_action=(
            "Mark it on demand if it only runs sometimes, or remove its declaration."
        ),
    ),
    FindingRule(
        "kind-never-swept",
        "Kind not seen by the sweep",
        "serious",
        _kind_never_swept,
        operator_action=(
            "Check the connection that reads this kind, then request a fresh sweep."
        ),
        # When the sweep itself is the fault, every record of the kind looks
        # skipped. Saying it once about the kind beats saying it about each.
        subsumes=("skipped-by-a-sweep", "never-observed"),
    ),
    FindingRule(
        "reporting-a-fault",
        "Resource reports a fault",
        "serious",
        _reporting_a_fault,
        operator_action=(
            "Read the provider's message on the resource, fix the cause, then reconcile."
        ),
    ),
    FindingRule(
        "reconciled-but-still-wrong",
        "Still wrong after reconciling",
        "serious",
        _reconciled_but_still_wrong,
        operator_action=(
            "Correct the declaration so it describes what the provider can hold, then reconcile."
        ),
    ),
    FindingRule(
        "weakly-verified",
        "Unconfirmed fields",
        "attention",
        _weakly_verified,
        operator_action=(
            "Make the provider report these fields, or declare them unobservable on the kind."
        ),
    ),
    FindingRule(
        "never-observed",
        "Never seen",
        "attention",
        _never_observed,
        operator_action=(
            "Check that the record exists at the provider under its declared name, or remove the declaration."
        ),
    ),
)
