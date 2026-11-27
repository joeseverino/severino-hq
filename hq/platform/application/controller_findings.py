"""Findings about the controller's own work: kinds it stopped sweeping, records it skipped or never saw, and changes it keeps applying without effect."""

import re
from dataclasses import replace
from datetime import date

from hq.domains.control_plane.observations.host import CONTROLLER_UNIT, SECRETS_UNIT
from hq.domains.control_plane.observations.portainer import CONTAINER_KIND, ENVIRONMENT_KIND
from hq.domains.control_plane.providers import PROVIDERS, unswept

from .cadence import CONTROLLER_REFRESH, sweep_interval
from .credential_findings import UNREPORTED
from .delivery_progress import LABELS as DEPLOYING_LABELS
from .derivations import since
from .entity_links import kind_label, node_link, node_name
from .finding_model import (
    Finding,
    FindingEstate,
    FindingRule,
    Said,
    cannot_run_commands,
    fact_values,
    per_connection,
    reconcile_remedy,
)
from .finding_steps import ByHand, Does, Fix, Why
from .freshness import LAST_READ, OUT_OF_DATE, behind, freshness
from .infrastructure import DRIFT_LABEL, NOT_READ_LABEL
from .labels import field_label, lower_first, plural
from .moments import duration, when
from .reading_facts import refusals
from .tone import Tone
from .topology_model import TopologyNode
from .ui import counted, ended
lazy from .facts import inventory_records
lazy from .moments import moment
lazy from .unit_findings import on_unit


def _many(kind_key: str) -> str:
    """A type of record as a sentence starts with it: "Public DNS records"."""

    provider = PROVIDERS.get(kind_key)
    return (provider.label_plural if provider else "") or plural(kind_label(kind_key))


def _when(label: str, at: date | str | None) -> tuple[str, str]:
    """A "last read" or "last seen" fact: how long ago, or never."""

    return (label, "never" if at is None else when(at))


def _read_again(variant: str = "") -> Does:
    return Does(CONTROLLER_REFRESH.name, variant=variant)


def _skipped_by_a_sweep(estate: FindingEstate) -> tuple[Finding, ...]:
    """Last seen materially earlier than its kind's last read.

    A sweep confirms everything it matches in one pass, so a read and the
    records it found land together. One left behind was not slow, it was skipped,
    and being skipped is invisible in every other surface, because the thing
    keeps whatever it last said about itself.
    """

    found = []
    # A machine whose whole environment read empty is one claim about the
    # environment (``_environment_read_empty``), never one per container.
    emptied = {node.id for nodes in _emptied_machines(estate).values() for node in nodes}
    for node in estate.nodes():
        if node.id in emptied or not _left_behind(estate, node):
            continue
        newest = estate.latest_by_kind[node.kind_key]
        siblings = sum(1 for other in estate.nodes() if other.kind_key == node.kind_key and other.id in estate.observed)
        others = siblings - 1
        noun = lower_first(node.subtitle)
        found.append(
            Finding(
                rule="skipped-by-a-sweep",
                subject=node.id,
                title=f"{node_name(node)} was missing from HQ's last read",
                severity=Tone.SERIOUS,
                explanation="What HQ shows of it is what it said before that read.",
                evidence=(
                    _when("Last seen", node.observed_at),
                    _when("Others read" if others else LAST_READ, newest),
                    *((("Others", counted(others, noun, plural(noun))),) if others else ()),
                ),
                fixes=_skipped_remedies(node),
            )
        )
    return tuple(found)


def _left_behind(estate: FindingEstate, node: TopologyNode) -> bool:
    """Whether a record HQ watches was last seen materially before its kind's last read."""

    return (
        node.managed
        and not node.on_demand
        and behind(estate.observed.get(node.id), estate.latest_by_kind.get(node.kind_key))
    )


def _emptied_machines(estate: FindingEstate) -> dict[str, tuple[TopologyNode, ...]]:
    """Each machine whose containers a read listed before and the last read
    listed none of, with those containers, by machine id.

    A machine's containers are read through one Portainer environment, so
    every one of them left behind at once is that environment answering with
    nothing, which says nothing about any one container. One still listed
    means the environment answered, and the others are each their own claim.
    """

    by_id = {node.id: node for node in estate.nodes()}
    held: dict[str, list[TopologyNode]] = {}
    for edge in estate.topology.edges:
        node = by_id.get(edge.target)
        if edge.kind != "runs" or node is None or node.kind_key != CONTAINER_KIND:
            continue
        if node.id in estate.observed and node.managed and not node.on_demand:
            held.setdefault(edge.source, []).append(node)
    return {
        machine: tuple(sorted(nodes, key=lambda node: node.label))
        for machine, nodes in held.items()
        if machine in by_id and all(_left_behind(estate, node) for node in nodes)
    }


# What reaches Docker for an environment, and so what to restart when it
# answers with nothing: Portainer itself for its local socket, the agent on
# the machine for an agent environment. Each is found by the label its image
# carries (Portainer's and the agent's Dockerfiles), so the command names no
# container the empty read could not.
_PORTAINER = ("Portainer", "io.portainer.server=true")
_AGENT = ("the Portainer agent", "io.portainer.agent=true")
_AGENT_TYPES = frozenset({"agent", "edge agent"})


def _environment_on(machine: str) -> dict:
    """The Portainer environment the last read filed on ``machine``, or none."""

    return next(
        (dict(record) for _snapshot, record in inventory_records(ENVIRONMENT_KIND) if record.get("host") == machine),
        {},
    )


def _holder(environment: dict) -> tuple[str, str]:
    """What reads Docker for this environment, and the label that finds its container."""

    if environment.get("local"):
        return _PORTAINER
    return _AGENT if environment.get("type") in _AGENT_TYPES else ("", "")


def _environment_read_empty(estate: FindingEstate) -> tuple[Finding, ...]:
    """An environment whose containers were listed, answering with none.

    Docker restarting under Portainer leaves whatever mounted its socket
    holding one that no longer answers, and Portainer then lists nothing while
    every container runs on. Restarting what holds the socket brings it back.
    """

    by_id = {node.id: node for node in estate.nodes()}
    newest = estate.latest_by_kind.get(CONTAINER_KIND)
    found = []
    for machine_id, containers in sorted(_emptied_machines(estate).items()):
        machine = by_id[machine_id].label
        environment = _environment_on(machine)
        name = str(environment.get("name", "") or "")
        holder, label = _holder(environment)
        last = max(estate.observed[node.id] for node in containers)
        restart = (
            ByHand.on(
                machine,
                f"Restart {holder} on {machine}, so it opens the Docker socket again",
                f"sh -c 'docker restart $(docker ps -q --filter label={label})'",
                Why.CHANNEL,
            )
            if holder
            else ()
        )
        found.append(
            Finding(
                rule="environment-read-empty",
                subject=machine_id,
                # A refresh reads the containers, which is what proves it.
                scope=CONTAINER_KIND,
                part=name,
                title=(
                    f"Portainer's {name} environment on {machine} lists no containers"
                    if name and name != machine
                    else f"Portainer lists no containers on {machine}"
                ),
                severity=Tone.SERIOUS,
                # Its step names what holds the socket, where HQ can name it.
                explanation=" ".join(
                    (
                        "What holds Portainer's Docker socket keeps one that no longer answers.",
                        *(() if holder else ("Restart what gives Portainer its Docker socket.",)),
                    )
                ),
                evidence=(
                    ("Environment", name or machine),
                    *((("Status", str(environment["status"])),) if environment.get("status") else ()),
                    ("Listed until", when(last, estate.now)),
                    *((_when(LAST_READ, newest),) if newest else ()),
                    *(("Not listed", node_link(node)) for node in containers),
                ),
                fixes=(_read_again(), *restart),
                no_help_reason=cannot_run_commands(machine),
            )
        )
    return tuple(found)


def _skipped_remedies(node: TopologyNode) -> tuple[Does, ...]:
    """What to do about a record the last read missed, as the actions that do it.

    A record missing from a sweep is gone: HQ can create it again from its
    config. A container is either one that runs now and then, or one that is
    gone, and HQ cannot create a container; nor a record whose declaration
    only says what HQ watches. Removing the record destroys, so it is never
    a press here.
    """

    if node.kind_key == CONTAINER_KIND:
        return (Does("infrastructure.resource.update", node.label, intent="Mark it as running only sometimes"),)
    provider = PROVIDERS.get(node.kind_key)
    if provider is not None and provider.declaration_only:
        return ()
    return tuple(replace(remedy, intent="Create it again") for remedy in reconcile_remedy(node))


REFRESH_OR_FIX = "Refresh them, or fix the connection that reads them."


def _kind_never_swept(estate: FindingEstate) -> tuple[Finding, ...]:
    """A whole kind that nothing has observed lately, which the sibling
    comparison above cannot see: a kind with one record has nothing to be
    behind, and one where every record is equally stale looks consistent.
    """

    found = []
    # Deliberately blunt, an absolute clock against the interval HQ itself
    # declares, since that is the only thing left to compare against. Out of
    # date is ``freshness``'s word, so a page and this claim cannot disagree.
    for kind_key, newest in sorted(estate.latest_by_kind.items()):
        if not freshness(kind_key, newest, estate.now).stale or unswept(kind_key):
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
                severity=Tone.SERIOUS,
                explanation=REFRESH_OR_FIX,
                evidence=(
                    _when(LAST_READ, newest),
                    ("Read every", duration(sweep_interval())),
                ),
                fixes=(_read_again(),),
            )
        )
    # A kind with no observation at all has no newest to be behind, so the loop
    # above cannot see it. It is claimed once about the kind rather than once
    # per record, which would bury the queue in copies of one claim.
    for kind_key in sorted(estate.declared_kinds - set(estate.latest_by_kind)):
        if unswept(kind_key):
            continue
        found.append(
            Finding(
                rule="kind-never-swept",
                subject="",
                scope=kind_key,
                title=f"{_many(kind_key)} have never been read",
                severity=Tone.SERIOUS,
                explanation=REFRESH_OR_FIX,
                evidence=(
                    _when(LAST_READ, None),
                    ("In HQ", str(estate.declared_counts.get(kind_key, 0))),
                    ("Read every", duration(sweep_interval())),
                ),
                fixes=(_read_again(),),
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
        title=f"The controller on {machine} stopped reading",
        severity=Tone.SERIOUS,
        explanation="What HQ shows of what it reads is what it last read.",
        evidence=(
            ("Not read", ", ".join(sorted(_many(kind) for kind in kinds))),
            *((_when(LAST_READ, last),) if last else ()),
        ),
        fixes=(_read_again("all"), *_controller_steps(machine)),
        no_help_reason=cannot_run_commands(machine),
        affected_scopes=tuple(sorted(kinds)),
    )


def _controller_steps(machine: str) -> tuple[ByHand, ...]:
    """How the owner looks at the controller on its machine."""

    return (
        *ByHand.on(
            machine,
            "Check the controller is running",
            f"systemctl status {CONTROLLER_UNIT} --no-pager",
            Why.NOT_GRANTED,
        ),
        *ByHand.journal(machine, "Read its log", CONTROLLER_UNIT),
    )


def _keep_live(node: TopologyNode) -> Does:
    return Does("infrastructure.resource.accept_observed", node.label)


def drift_evidence(node: TopologyNode) -> tuple[tuple[str, str], ...]:
    """When the drift was first seen, and what happened near then, from the
    facts the topology carries (``topology_facts._drift_facts``)."""

    since = fact_values(node, "drift-since")
    if not since:
        return ()

    first = moment(since[0])
    near = fact_values(node, "drift-near")
    return (
        ("Changed", when(first) if first else since[0]),
        *(("What else happened around then", item) for item in near),
        *(
            (("What else happened around then", "Nothing else was recorded in the six hours before or after."),)
            if not near
            else ()
        ),
    )


def _fault_said(node: TopologyNode, otherwise: tuple[str, str]) -> tuple[str, str]:
    """What the resource itself says is wrong, as a title as far as its first
    sentence and what it says after that; a resource that says nothing, or
    too much to title, gets ``otherwise``'s title, and one that says no more
    than its title gets ``otherwise``'s reason."""

    said = node.detail.strip()
    if not said:
        return otherwise
    first, *rest = re.split(r"(?<=[.!?])\s", said, maxsplit=1)
    first = first.rstrip(".")
    # "what failed: the detail" is headed by what failed. "name: what is wrong
    # with it" is one statement, and its name alone would say nothing.
    lead, colon, after = first.partition(": ")
    if colon and " " in lead:
        first, rest = lead, [ended(after), *rest]
    if len(first) > 110:
        return otherwise[0], said
    return first, " ".join(rest) or otherwise[1]


def _fault_evidence(node: TopologyNode) -> tuple[tuple[str, str], ...]:
    return (
        ("What", _what(node, node_name(node))),
        ("Status", node.status_label or node.status),
        *drift_evidence(node),
    )


def _what(node: TopologyNode, name: str) -> str:
    """A record named its kind, then its name, unless the name already says
    the kind."""

    if not node.kind_key or kind_label(node.kind_key).casefold() in name.casefold():
        return name
    return f"{kind_label(node.kind_key)} {name}"


def _fault_remedies(node: TopologyNode) -> tuple[Does, ...]:
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
    no config of HQ's differs from it, and applying, keeping or restoring
    changes nothing about it."""

    provider = PROVIDERS.get(node.kind_key)
    return bool(provider and provider.reported_fields)


def _cannot_fix_report(node: TopologyNode) -> str:
    return f"HQ cannot fix what {node_name(node)} reports." if _reports_itself(node) else ""


def _reporting_a_fault(estate: FindingEstate) -> tuple[Finding, ...]:
    """A resource whose own condition says it is wrong, now.

    `reconciled-but-still-wrong` covers the case where a reconcile has already
    been tried against this exact declaration. This is the rest: a fault the
    resource is reporting while a change is still outstanding, or one that is
    its own report, which no config of HQ's differs from.
    """

    return tuple(
        Finding(
            rule="reporting-a-fault",
            subject=node.id,
            title=title,
            severity=Tone.SERIOUS if node.status == Tone.SERIOUS else Tone.ATTENTION,
            explanation=explanation,
            evidence=_fault_evidence(node),
            fixes=_fault_remedies(node),
            no_help_reason=_cannot_fix_report(node),
        )
        for node in estate.nodes()
        if node.kind == "resource"
        and node.managed
        and node.status in {Tone.ATTENTION, Tone.SERIOUS}
        # A deploy on its way, or held for a reviewer, is no fault: the
        # delivery's own findings and github-waiting say it.
        and node.status_label not in DEPLOYING_LABELS.values()
        # The other rule owns the converged case and says something sharper
        # about it, so this one takes what is left rather than doubling it.
        and (node.declared_revision != node.observed_revision or _reports_itself(node))
        for title, explanation in (
            _fault_said(node, (f"{node_name(node)} reports an error", "Your last change to it is not applied yet.")),
        )
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
            title=title,
            severity=Tone.SERIOUS,
            explanation=explanation,
            evidence=_fault_evidence(node),
            # Applying again is the one thing already known not to work. Where
            # the live record was changed, the choice is which side to keep.
            fixes=(
                (
                    _keep_live(node),
                    Does("infrastructure.resource.update", node.label),
                )
                if node.status_label == DRIFT_LABEL
                else ()
            ),
        )
        for node in estate.nodes()
        if node.kind == "resource"
        and node.managed
        and node.status in {Tone.ATTENTION, Tone.SERIOUS}
        and node.declared_revision == node.observed_revision
        and node.declared_revision > 0
        and not _reports_itself(node)
        for title, explanation in (
            _fault_said(
                node, (f"{node_name(node)} still differs after HQ applied it", "Applying again will not help.")
            ),
        )
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
            severity=Tone.ATTENTION,
            explanation="Check its name on the service.",
            evidence=(_when("Last seen", None), _when("Others read", estate.latest_by_kind[node.kind_key])),
            fixes=reconcile_remedy(node),
        )
        for node in estate.nodes()
        if node.kind == "resource"
        and node.managed
        and not node.observed_at
        and not unswept(node.kind_key)
        and node.id in estate.governed
        # The kind has been read, so the sweep can see it and missed this
        # one. Without that, the gap is the kind's, and saying it per record
        # buries the one claim worth reading.
        and node.kind_key in estate.latest_by_kind
    )


def _unchecked(estate: FindingEstate) -> tuple[Finding, ...]:
    """A record whose kind HQ reads that reports no state of its own, or whose
    last reading is out of date, so HQ can vouch for it neither way. One the sweep missed, never found, read without
    the settings HQ set, or of a kind not read lately is those rules' to say,
    which subsume this."""

    return tuple(
        Finding(
            rule="resource-unchecked",
            subject=node.id,
            title=f"{_what(node, node.label)} could not be checked",
            severity=Tone.ATTENTION,
            explanation=(
                "Its last reading is too old to say how it stands now."
                if node.status_label == OUT_OF_DATE
                else "Its reading reports no state of its own."
            ),
            evidence=(_when(LAST_READ, node.observed_at or None),),
            fixes=reconcile_remedy(node),
        )
        for node in estate.nodes()
        if node.kind == "resource" and node.managed and node.status_label in (NOT_READ_LABEL, OUT_OF_DATE)
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
            title=f"HQ could not confirm {counted(len(node.unconfirmed_fields), 'setting')} of {node_name(node)}",
            severity=Tone.ATTENTION,
            explanation="Apply HQ's config to read them back.",
            evidence=(
                ("Could not check", _settings(node)),
                _when(LAST_READ, node.observed_at),
            ),
            fixes=reconcile_remedy(node),
        )
        for node in estate.nodes()
        if node.kind == "resource" and node.managed and node.unconfirmed_fields
    )


def _settings(node: TopologyNode) -> str:
    """The settings HQ could not check, by the titles their form gives them."""

    provider = PROVIDERS.get(node.kind_key)
    fields = provider.spec_type.model_fields if provider else {}
    return ", ".join(
        lower_first(field_label(name, fields[name].title if name in fields else None))
        for name in node.unconfirmed_fields
    )


def _work_that_keeps_failing(estate: FindingEstate) -> tuple[Finding, ...]:
    """A connection HQ can open and cannot use.

    A connection can pass every probe while all work sent through it refuses,
    and the page says "reachable" the whole time. The code that sends the work
    catches the failures, so they never become an operation anybody sees and
    repeat every pass for as long as nobody reads a log.
    """

    machines = _controller_machines(estate)
    return per_connection(
        estate,
        "work-unfinished",
        rule="work-that-keeps-failing",
        severity=Tone.ATTENTION,
        shown_as="Keeps failing",
        says=lambda node, unfinished: _keeps_failing(node, unfinished, machines.get(node.id, "")),
    )


def _keeps_failing(node: TopologyNode, unfinished: tuple[str, ...], machine: str) -> Said:
    return {
        "title": f"Work through {node.label} keeps failing",
        "explanation": "The controller retries it every few minutes, and each try has failed.",
        "fixes": ByHand.journal(machine, "Read the controller's log", CONTROLLER_UNIT),
    }


def _connections_lost(estate: FindingEstate) -> tuple[Finding, ...]:
    """A controller that reported connections, and now reports none.

    A machine whose runtime directory was cleared installs the empty set a new
    host would, which the refresh job cannot keep from happening. HQ keeps the
    rows that report would have removed, marked, so it is the one place that
    still knows there were connections; a host that never had one raises nothing.
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
        since = when(min(fact_values(connection, UNREPORTED)[0] for connection in connections), estate.now)
        found.append(
            Finding(
                rule="connections-lost",
                subject=node.id,
                title=f"The controller on {node.label} reports no connections",
                severity=Tone.SERIOUS,
                explanation="Check that this machine's vault still holds their items.",
                evidence=(
                    *(("Last reported", node_link(each)) for each in sorted(connections, key=lambda each: each.label)),
                    ("None since", since),
                ),
                fixes=(_read_again(), *_refresh_steps(node.label)),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


def _refresh_steps(machine: str) -> tuple[Fix, ...]:
    """How the owner runs the refresh job again on its machine and reads what it wrote."""

    unit = SECRETS_UNIT
    return (
        *on_unit(
            "infrastructure.resource.start",
            unit,
            ByHand.on(machine, "Run the refresh job now", f"systemctl start {unit}", Why.NOT_GRANTED),
            intent="Run the refresh job now",
        ),
        *ByHand.journal(machine, "Read the refresh job's log", unit),
        *_controller_steps(machine),
    )


def _report_refused(estate: FindingEstate) -> tuple[Finding, ...]:
    """A call of the controller's that the bridge refuses, until one is accepted.

    A refusal runs nothing of what it carried, so nothing else changes: its
    readings only age, and every surface reads them as a quiet estate. The
    refusal says where the call departs from the contract, never what it held.
    """

    return tuple(
        Finding(
            rule="report-refused",
            subject=node.id,
            part=refusal.action,
            title=f"HQ refuses the controller's {refusal.action} report from {node.label}",
            severity=Tone.SERIOUS,
            explanation="Deploy HQ so it and the controller match.",
            evidence=(
                ("Report", refusal.action),
                ("Reason", refusal.detail),
                ("Last refused", when(refusal.refused_at, estate.now)),
            ),
            fixes=_controller_steps(node.label),
            no_help_reason=cannot_run_commands(node.label),
        )
        for node, held in refusals(estate)
        for refusal in held
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
        "report-refused",
        "Controller report refused",
        Tone.SERIOUS,
        _report_refused,
        gathered=True,
        operator_action="Deploy HQ so it and the controller match. If it stays, fix what the reason names.",
        no_help_reason="HQ cannot change what the controller sends.",
        bars={Tone.SERIOUS: "HQ refuses the controller's report, so nothing new is read"},
    ),
    FindingRule(
        "connections-lost",
        "No connections reported",
        Tone.SERIOUS,
        _connections_lost,
        operator_action=(
            f"Check the machine's vault holds the connection items, run the refresh job, then press "
            f"{CONTROLLER_REFRESH.words()}."
        ),
        no_help_reason="HQ cannot read the vault.",
        bars={Tone.SERIOUS: "the controller reports no connections at all"},
    ),
    FindingRule(
        "work-that-keeps-failing",
        "Work that keeps failing",
        Tone.ATTENTION,
        _work_that_keeps_failing,
        operator_action="Read the controller's log and fix what it names.",
        no_help_reason="HQ cannot read the controller's log.",
        bars={Tone.ATTENTION: "work through a connection that answers keeps failing"},
    ),
    FindingRule(
        "controller-sweep-stale",
        "Controller stopped reading",
        Tone.SERIOUS,
        _controller_sweep_stale,
        operator_action=f"Check the controller is running, then press {CONTROLLER_REFRESH.words('all')}.",
        no_help_reason="HQ cannot start the controller.",
        subsumes=("kind-never-swept",),
        bars={Tone.SERIOUS: "one controller is behind on two or more types of record"},
    ),
    FindingRule(
        "environment-read-empty",
        "Docker lists no containers",
        Tone.SERIOUS,
        _environment_read_empty,
        operator_action="Restart Portainer, or its agent, on the machine.",
        no_help_reason="HQ restarts containers through Portainer, which cannot reach Docker there.",
        bars={Tone.SERIOUS: "a Docker environment that had containers lists none"},
    ),
    FindingRule(
        "skipped-by-a-sweep",
        "Missing from HQ's last read",
        Tone.SERIOUS,
        _skipped_by_a_sweep,
        operator_action="Remove it from HQ if it is gone, or mark it as running only sometimes.",
        no_help_reason="HQ cannot tell whether it was removed on purpose.",
        subsumes=("resource-unchecked",),
        bars={Tone.SERIOUS: "the last read found the others but not it"},
    ),
    FindingRule(
        "kind-never-swept",
        "Not read lately",
        Tone.SERIOUS,
        _kind_never_swept,
        operator_action=f"Fix the connection that reads them, then press {CONTROLLER_REFRESH.words()}.",
        no_help_reason="HQ cannot fix a connection that does not answer.",
        # When the sweep itself is the fault, every record of the kind looks
        # skipped. Saying it once about the kind beats saying it about each.
        subsumes=("skipped-by-a-sweep", "never-observed", "environment-read-empty", "resource-unchecked"),
        bars={Tone.SERIOUS: "a type of record has gone unread past its interval"},
    ),
    FindingRule(
        "reporting-a-fault",
        "Errors reported",
        Tone.SERIOUS,
        _reporting_a_fault,
        operator_action="Open it, fix the error, then apply again.",
        no_help_reason="HQ cannot tell what to change from the error.",
        bars={
            Tone.SERIOUS: "the service reports a serious error",
            Tone.ATTENTION: "the service reports an error",
        },
    ),
    FindingRule(
        "reconciled-but-still-wrong",
        "Differs from HQ's config",
        Tone.SERIOUS,
        _reconciled_but_still_wrong,
        operator_action="Change HQ's config to one the service accepts, then apply again.",
        no_help_reason="HQ cannot tell which setting was refused.",
        bars={Tone.SERIOUS: "HQ applied its config and the service still differs"},
    ),
    FindingRule(
        "weakly-verified",
        "Unconfirmed settings",
        Tone.ATTENTION,
        _weakly_verified,
        operator_action="Apply HQ's config to read them back.",
        no_help_reason="HQ cannot read this setting from the service.",
        subsumes=("resource-unchecked",),
        bars={Tone.ATTENTION: "HQ cannot read back a setting it holds"},
    ),
    FindingRule(
        "never-observed",
        "Records never found",
        Tone.ATTENTION,
        _never_observed,
        operator_action="Check its exact name on the service, or remove it from HQ.",
        no_help_reason="HQ cannot tell whether it exists under another name.",
        subsumes=("resource-unchecked",),
        bars={Tone.ATTENTION: "a record in HQ was never found on its service"},
    ),
    FindingRule(
        "resource-unchecked",
        "Records not checked",
        Tone.ATTENTION,
        _unchecked,
        operator_action="Apply HQ's config to it to read it back.",
        no_help_reason="HQ does not change this type. Change it at its source.",
        bars={Tone.ATTENTION: "HQ has no way to check a record against its service"},
    ),
)
