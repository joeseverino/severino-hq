"""Findings about the controller's own work: kinds it stopped sweeping, records it skipped or never saw, and changes it keeps applying without effect."""

import re
from datetime import date

from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.domains.control_plane.providers import PROVIDERS

from .cadence import sweep_interval
from .core_capabilities import CONTROLLER_REFRESH
from .credential_findings import UNREPORTED
from .derivations import since
from .entity_links import kind_label, node_name
from .finding_model import (
    Finding,
    FindingEstate,
    FindingRemedy,
    FindingRule,
    cannot_run_commands,
    fact_values,
    is_observable,
    journal_step,
    machine_step,
    reconcile_remedy,
)
from .freshness import freshness
from .infrastructure import DRIFT_LABEL
from .labels import lower_first, plural
from .moments import ago, duration
from .render_status_findings import RENDERER_UNITS
from .topology_lenses import _STALE_AFTER
from .topology_model import TopologyNode
from .ui import counted
lazy from .timestamps import moment

# The background job that runs the controller on its machine (deploy/systemd).
CONTROLLER_UNIT = "severino-hq-controller.service"


def _many(kind_key: str) -> str:
    """A type of record as a sentence starts with it: "Public DNS records"."""

    provider = PROVIDERS.get(kind_key)
    return (provider.label_plural if provider else "") or plural(kind_label(kind_key))


def _the_others(count: int, noun: str) -> str:
    """The rest of a record's type as the start of a sentence about them."""

    if count == 1:
        return f"The one other {noun} was"
    return f"The other {count} {plural(noun)} were"


def _when(label: str, at: date | str | None) -> tuple[str, str]:
    """A "last read" or "last seen" fact: how long ago, or never."""

    return (label, "never" if at is None else ago(at))


def _read_again(variant: str = "") -> FindingRemedy:
    return FindingRemedy(capability=CONTROLLER_REFRESH.name, target="", variant=variant)


# ``_STALE_AFTER`` is imported rather than restated: the lens that asks "what
# was left behind?" and the rule that claims it must not be able to disagree
# about where that line falls.


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
        siblings = sum(1 for other in estate.nodes() if other.kind_key == node.kind_key and other.id in estate.observed)
        name = node_name(node)
        others = siblings - 1
        found.append(
            Finding(
                rule="skipped-by-a-sweep",
                subject=node.id,
                title=f"{name} was not found the last time HQ looked",
                severity="serious",
                explanation=(
                    f"{_the_others(others, lower_first(node.subtitle))} read {ago(newest)}. This one was "
                    f"last seen {duration(behind)} earlier, so it has probably been "
                    "removed or renamed."
                ),
                evidence=(
                    _when("Last seen", node.observed_at),
                    _when("Others read", newest),
                ),
                remedies=_skipped_remedies(node),
            )
        )
    return tuple(found)


def _skipped_remedies(node: TopologyNode) -> tuple[FindingRemedy, ...]:
    """The two answers the explanation gives, as the actions that carry them out.

    A container missing from a sweep is either one that runs now and then, or
    one that is gone. Reconciling answers neither: it asks the provider to look
    again at something that is, correctly, not running.
    """

    remove = FindingRemedy(capability="infrastructure.resource.remove", target=node.label)
    if node.kind_key == CONTAINER_KIND:
        return (
            FindingRemedy(
                capability="infrastructure.resource.update",
                target=node.label,
                intent="Mark it as running only sometimes",
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
    Out of date means what it means on every page that shows the reading
    (``freshness``), so a page and this claim cannot disagree about it.
    """

    found = []
    for kind_key, newest in sorted(estate.latest_by_kind.items()):
        if not freshness(kind_key, newest, estate.now).stale or not is_observable(kind_key):
            continue
        silent = since(newest, now=estate.now)
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
                title=f"{_many(kind_key)} have not been read for {duration(silent)}",
                severity="serious",
                explanation="Refresh them. If that fails, the connection that reads them needs fixing.",
                evidence=(
                    _when("Last read", newest),
                    ("Read every", duration(sweep_interval())),
                ),
                remedies=(_read_again(),),
            )
        )
    # A kind with no observation at all has no newest to be behind, so the loop
    # above cannot see it. It is claimed once about the kind rather than once
    # per record, which would bury the queue in copies of one claim.
    for kind_key in sorted(estate.declared_kinds - set(estate.latest_by_kind)):
        if not is_observable(kind_key):
            continue
        found.append(
            Finding(
                rule="kind-never-swept",
                subject="",
                scope=kind_key,
                title=f"{_many(kind_key)} have never been read",
                severity="serious",
                explanation=(
                    "What HQ shows for them is only what was entered in HQ. Refresh "
                    "them. If that fails, the connection that reads them needs fixing."
                ),
                evidence=(
                    _when("Last read", None),
                    ("In HQ", str(estate.declared_counts.get(kind_key, 0))),
                    ("Read every", duration(sweep_interval())),
                ),
                remedies=(_read_again(),),
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

    stale_kinds = {finding.scope for finding in _kind_never_swept(estate) if finding.scope}
    grouped: dict[str, set[str]] = {}
    for kind in stale_kinds:
        for controller in estate.controllers_by_kind.get(kind, frozenset()):
            grouped.setdefault(controller, set()).add(kind)
    by_id = {node.id: node for node in estate.nodes()}
    return tuple(
        _controller_stopped(estate, by_id[controller].label, controller, kinds)
        for controller, kinds in sorted(grouped.items())
        if len(kinds) >= 2 and controller in by_id
    )


def _controller_stopped(estate: FindingEstate, machine: str, controller: str, kinds: set[str]) -> Finding:
    """One card for a controller that has stopped reading several types of record."""

    last = max((estate.latest_by_kind[kind] for kind in kinds if kind in estate.latest_by_kind), default=None)
    return Finding(
        rule="controller-sweep-stale",
        subject=controller,
        title=(
            f"The controller on {machine} has not read "
            f"{counted(len(kinds), 'type of record', 'types of record')}"
            + (f" for {duration(since(last, now=estate.now))}" if last else "")
        ),
        severity="serious",
        explanation="Everything listed is read by the same controller, so check it first.",
        evidence=(("Not read", ", ".join(sorted(_many(kind) for kind in kinds))),),
        remedies=(_read_again("all"),),
        steps=_controller_steps(machine),
        no_help_reason=cannot_run_commands(machine),
        affected_scopes=tuple(sorted(kinds)),
    )


def _controller_steps(machine: str):
    """How the owner looks at the controller on its machine."""

    return (
        *machine_step(
            "Check the controller is running",
            machine,
            f"systemctl status {CONTROLLER_UNIT} --no-pager",
        ),
        *journal_step("Read its log", machine, CONTROLLER_UNIT),
    )


def _keep_live(node: TopologyNode) -> FindingRemedy:
    return FindingRemedy(capability="infrastructure.resource.accept_observed", target=node.label)


def drift_evidence(node: TopologyNode) -> tuple[tuple[str, str], ...]:
    """When the drift was first seen, and what happened near then, from the
    facts the topology carries (``topology_facts._drift_facts``)."""

    since = fact_values(node, "drift-since")
    if not since:
        return ()

    first = moment(since[0])
    near = fact_values(node, "drift-near")
    return (
        ("Changed", ago(first) if first else since[0]),
        *(("What else happened around then", item) for item in near),
        *(
            (("What else happened around then", "Nothing else was recorded in the six hours before or after."),)
            if not near
            else ()
        ),
    )


def _fault_title(node: TopologyNode, otherwise: str) -> str:
    """What the resource itself says is wrong, as far as its first sentence;
    a resource that says nothing gets ``otherwise``."""

    said = node.detail.strip()
    if not said:
        return otherwise
    first = re.split(r"(?<=[.!?])\s", said, maxsplit=1)[0].rstrip(".")
    # "what failed: the detail" is headed by what failed. "name: what is wrong
    # with it" is one statement, and its name alone would say nothing.
    lead, colon, _rest = first.partition(": ")
    if colon and " " in lead:
        first = lead
    return first if len(first) <= 110 else otherwise


def _fault_evidence(node: TopologyNode) -> tuple[tuple[str, str], ...]:
    return (
        ("What", f"{kind_label(node.kind_key)} {node_name(node)}" if node.kind_key else node.label),
        ("Status", node.status_label or node.status),
        *drift_evidence(node),
    )


def _fault_remedies(node: TopologyNode) -> tuple[FindingRemedy, ...]:
    """Drift has two honest answers, and reconciling alone is the destructive one.

    Something changed the live record outside HQ. Reconciling pushes HQ's copy
    over it; keeping it copies the change into HQ. Which is right is the
    operator's call, so both are offered, the one that loses nothing first.
    """

    if _reports_itself(node):
        return ()
    restore = reconcile_remedy(node)
    if node.status_label != DRIFT_LABEL:
        return restore
    return (_keep_live(node), *restore)


def _reports_itself(node: TopologyNode) -> bool:
    """Whether what is wrong is the thing's own report (``ProviderSpec.reported_fields``):
    applying, keeping or restoring changes nothing about it."""

    provider = PROVIDERS.get(node.kind_key)
    return bool(provider and provider.reported_fields)


def _cannot_fix_report(node: TopologyNode) -> str:
    return f"HQ cannot fix what {node_name(node)} reports." if _reports_itself(node) else ""


def _reporting_a_fault(estate: FindingEstate) -> tuple[Finding, ...]:
    """A resource whose own condition says it is wrong, now.

    `reconciled-but-still-wrong` covers the case where a reconcile has already
    been tried against this exact declaration. This is the rest: a fault the
    resource is reporting while a change is still outstanding.
    """

    return tuple(
        Finding(
            rule="reporting-a-fault",
            subject=node.id,
            title=_fault_title(node, f"{node_name(node)} reports an error"),
            severity="serious" if node.status == "serious" else "attention",
            explanation=node.detail or "It reports an error, and your last change to it has not been applied.",
            evidence=_fault_evidence(node),
            remedies=_fault_remedies(node),
            no_help_reason=_cannot_fix_report(node),
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
            title=_fault_title(node, f"{node_name(node)} is not set the way HQ last set it"),
            severity="serious",
            explanation=node.detail
            or (
                f"HQ applied its settings and {node_name(node)} still differs. Applying again "
                "will not help. Keep the live version, or change what HQ expects."
            ),
            evidence=_fault_evidence(node),
            no_help_reason=_cannot_fix_report(node),
            # Applying again is the one thing already known not to work. Where
            # the live record was changed, the choice is which side to keep.
            # Where the resource reports its own problem there is no side to
            # keep: what it says is the instruction.
            remedies=(
                (
                    _keep_live(node),
                    FindingRemedy(capability="infrastructure.resource.update", target=node.label),
                )
                if node.status_label == DRIFT_LABEL
                else ()
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
            title=f"{node_name(node)} has never been found",
            severity="attention",
            explanation=(
                f"HQ found the other {lower_first(_many(node.kind_key))} but never this one. "
                "What you see for it is only what was entered in HQ."
            ),
            evidence=(_when("Last seen", None),),
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
    read healthy, and be asserting a control nothing has ever checked. An
    unverified control is not a control, and staleness alone does not say so.
    """

    return tuple(
        Finding(
            rule="weakly-verified",
            subject=node.id,
            title=(f"HQ could not confirm {counted(len(node.unconfirmed_fields), 'setting')} of {node_name(node)}"),
            severity="attention",
            explanation=(
                f"HQ set these and the last reading did not include them: {_settings(node)}. "
                "Apply HQ's settings to set them and read them back. If this stays after applying, "
                "HQ cannot read them from the service."
            ),
            evidence=(
                ("Could not check", _settings(node)),
                _when("Last read", node.observed_at),
            ),
            remedies=reconcile_remedy(node),
        )
        for node in estate.nodes()
        if node.kind == "resource" and node.managed and node.unconfirmed_fields
    )


def _settings(node: TopologyNode) -> str:
    """The settings HQ could not check, by the titles their form gives them."""

    provider = PROVIDERS.get(node.kind_key)
    fields = provider.spec_type.model_fields if provider else {}
    return ", ".join(
        (fields[name].title or name.replace("_", " ")).lower() if name in fields else name.replace("_", " ")
        for name in node.unconfirmed_fields
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

    machines = _controller_machines(estate)
    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        unfinished = fact_values(node, "work-unfinished")
        if not unfinished:
            continue
        machine = machines.get(node.id, "")
        found.append(
            Finding(
                rule="work-that-keeps-failing",
                subject=node.id,
                title=(
                    f"{node.label} connects, but "
                    f"{counted(len(unfinished), 'thing it does keeps failing', 'things it does keep failing')}"
                ),
                severity="attention",
                explanation=(
                    "It retries every few minutes and has not succeeded. The reason is "
                    f"in the controller's log{f' on {machine}' if machine else ''}."
                ),
                evidence=tuple(("Keeps failing", item) for item in unfinished),
                steps=journal_step("Read the controller's log", machine, CONTROLLER_UNIT),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _connections_lost(estate: FindingEstate) -> tuple[Finding, ...]:
    """A controller that reported connections, and now reports none.

    The refresh job keeps installed connections when the vault suddenly
    resolves none, but a machine whose runtime directory was cleared has
    nothing to keep, and installs the empty set a new host would. HQ keeps the
    rows such a report would have removed, marked, so it is the one place that
    still knows there were connections. A host that never had a connection has
    no rows, and nothing is raised.
    """

    by_id = {node.id: node for node in estate.nodes()}
    held: dict[str, list[TopologyNode]] = {}
    for edge in estate.topology.edges:
        connection = by_id.get(edge.target)
        if edge.kind == "carries" and edge.source in by_id and connection and fact_values(connection, UNREPORTED):
            held.setdefault(edge.source, []).append(connection)
    found: list[Finding] = []
    for controller_id, connections in sorted(held.items()):
        node = by_id[controller_id]
        names = ", ".join(sorted(connection.label for connection in connections))
        since = ago(min(fact_values(connection, UNREPORTED)[0] for connection in connections), estate.now)
        found.append(
            Finding(
                rule="connections-lost",
                subject=node.id,
                title=f"The controller on {node.label} reports no connections",
                severity="serious",
                explanation=(
                    f"It reported {counted(len(connections), 'connection', 'connections')} until {since}, "
                    "and none since, so nothing is read or changed through them. "
                    "Check that the vault this machine reads still holds their items."
                ),
                evidence=(("Last reported", names), ("None since", since)),
                remedies=(_read_again(),),
                steps=_refresh_steps(node.label),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


def _refresh_steps(machine: str):
    """How the owner runs the refresh job again on its machine and reads what it wrote."""

    unit = RENDERER_UNITS["hq"]
    return (
        *machine_step("Run the refresh job now", machine, f"systemctl start {unit}"),
        *journal_step("Read the refresh job's log", machine, unit),
        *_controller_steps(machine),
    )


def _controller_machines(estate: FindingEstate) -> dict[str, str]:
    """The machine whose controller holds each connection, by connection id."""

    by_id = {node.id: node for node in estate.nodes()}
    return {
        edge.target: by_id[edge.source].label
        for edge in estate.topology.edges
        if edge.kind == "carries" and edge.source in by_id
    }


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "connections-lost",
        "The controller reports no connections",
        "serious",
        _connections_lost,
        operator_action=(
            "Check that the vault this machine reads holds the connection items, "
            f"run the refresh job, then press {CONTROLLER_REFRESH.words()}."
        ),
        no_help_reason="HQ cannot read the vault.",
    ),
    FindingRule(
        "work-that-keeps-failing",
        "A connection works but something through it keeps failing",
        "attention",
        _work_that_keeps_failing,
        operator_action=("Read the controller's log for this connection and fix what it names."),
        no_help_reason="HQ cannot read the controller's log.",
    ),
    FindingRule(
        "controller-sweep-stale",
        "The controller has stopped reading",
        "serious",
        _controller_sweep_stale,
        operator_action=f"On its machine, check the controller is running, then press {CONTROLLER_REFRESH.words('all')}.",
        no_help_reason="HQ cannot start the controller.",
        subsumes=("kind-never-swept",),
    ),
    FindingRule(
        "skipped-by-a-sweep",
        "Not found the last time HQ looked",
        "serious",
        _skipped_by_a_sweep,
        operator_action=("If it is gone, remove it from HQ. If it only runs sometimes, mark it so."),
        no_help_reason="HQ cannot tell whether it was removed on purpose.",
    ),
    FindingRule(
        "kind-never-swept",
        "Something has not been read for days",
        "serious",
        _kind_never_swept,
        operator_action=f"Fix the connection that reads them, then press {CONTROLLER_REFRESH.words()}.",
        no_help_reason="HQ cannot fix a connection that is not answering.",
        # When the sweep itself is the fault, every record of the kind looks
        # skipped. Saying it once about the kind beats saying it about each.
        subsumes=("skipped-by-a-sweep", "never-observed"),
    ),
    FindingRule(
        "reporting-a-fault",
        "Something reports an error",
        "serious",
        _reporting_a_fault,
        operator_action="Open it, read the error, fix it, then apply again.",
        no_help_reason="HQ cannot tell what to change from the error alone.",
    ),
    FindingRule(
        "reconciled-but-still-wrong",
        "Differs from HQ's settings",
        "serious",
        _reconciled_but_still_wrong,
        operator_action=("Change the settings in HQ to something the service accepts, then apply again."),
        no_help_reason="HQ cannot tell which setting was refused.",
    ),
    FindingRule(
        "weakly-verified",
        "Settings HQ could not check",
        "attention",
        _weakly_verified,
        operator_action="Apply HQ's settings, so HQ sets them and reads them back.",
        no_help_reason="HQ cannot read this setting from the service.",
    ),
    FindingRule(
        "never-observed",
        "Added to HQ but never found",
        "attention",
        _never_observed,
        operator_action=("Check it exists on the service under this exact name, or remove it from HQ."),
        no_help_reason="HQ cannot tell whether it exists under another name.",
    ),
)
