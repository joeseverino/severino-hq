"""Facts a controller's reading of its own machine puts on that machine's node.

A reading such as ``host.render_status`` or ``host.unit`` is taken by a
controller of the machine it runs on. Each record becomes one fact on the node
of that controller, a row of words and instants joined in field order, and
the rules read the rows back. A reading that could not be taken is a row too,
so what went quiet is said and not dropped.

A reading says how things stood when it was taken, so a rule judges its rows
as of then. Once it is older than the slowest the controller ever reads,
``stated`` withholds its rows from every rule, and ``unjudged`` hands the
reading's own "not read" rule when it was taken and, where the bridge refused
the report that would have replaced it, why.
"""

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Any, ClassVar, Self

from .cadence import slowest_sweep_interval
from .core_capabilities import CONTROLLER_REFRESH
from .derivations import passed
from .finding_model import (
    Finding,
    FindingEstate,
    FindingRemedy,
    OperatorStep,
    cannot_run_commands,
    fact_values,
)
from .moments import when
from .timestamps import moment
from .topology_model import TopologyNode, derived_id
from .ui import ended
lazy from .facts import snapshots_of
lazy from .refused_reports import READINGS_ACTION, open_refusals

# Why a reading holds no record, when the reading itself is what failed.
UNREAD = "unread"
REFUSED = "refused"

# When a node's reading of one kind was taken, as ``kind|instant``.
TAKEN = "reading-taken"
# A call of the controller's the bridge refused, as JSON.
REFUSED_REPORT = "refused-report"


class FactRow:
    """A frozen dataclass of strings carried whole as one fact.

    The subclass is a ``@dataclass(frozen=True)`` whose fields all default to
    ``""`` and hold no ``|``; ``KEY`` is the fact it is stored under and
    ``KIND`` the reading it comes from.
    """

    KEY: ClassVar[str]
    KIND: ClassVar[str]

    @property
    def fact(self) -> tuple[str, str]:
        return self.KEY, "|".join(getattr(self, field.name) for field in fields(self))

    @classmethod
    def of(cls, value: str) -> Self:
        names = [field.name for field in fields(cls)]
        parts = (value.split("|") + [""] * len(names))[: len(names)]
        return cls(**dict(zip(names, parts, strict=True)))


def rows_of(
    snapshot: Any, row: Callable[[dict[str, Any]], FactRow], unread: Callable[[str], FactRow]
) -> Iterator[FactRow]:
    """What one stored reading says: a row per record, and the reading's own
    failure as a row ``unread`` makes from ``UNREAD`` or ``REFUSED``."""

    if not snapshot.reachable:
        yield unread(UNREAD)
        return
    for record in snapshot.records or ():
        yield row(record)
    if snapshot.error:
        yield unread(REFUSED)


def _on_controller(
    nodes: dict[str, TopologyNode], machine: Callable[[Any], str], controller: str, found: tuple[tuple[str, str], ...]
) -> None:
    """Put ``found`` on the node of ``controller``: the machine node it folded
    into, else a node of its own, so what it says is never dropped for want
    of somewhere to say it."""

    node_id = derived_id("controller", controller)
    if node_id not in nodes:
        node_id = machine(controller) or node_id
    node = nodes.get(node_id) or TopologyNode(
        id=node_id, kind="controller", label=controller or "The controller", subtitle="Controller"
    )
    nodes[node_id] = replace(node, facts=node.facts + found)


def state_on_controller(
    nodes: dict[str, TopologyNode],
    machine: Callable[[Any], str],
    row: type[FactRow],
    rows: Callable[[Any], Iterable[FactRow]],
) -> None:
    """One fact per row of ``row.KIND``, and when it was read, on the node of
    the controller that read it."""

    for snapshot in snapshots_of(row.KIND):
        found = tuple(item.fact for item in rows(snapshot))
        if not found:
            continue
        if snapshot.observed_at:
            found += ((TAKEN, f"{row.KIND}|{snapshot.observed_at.isoformat()}"),)
        _on_controller(nodes, machine, snapshot.controller_id, found)


def add_refusals(nodes: dict[str, TopologyNode], machine: Callable[[Any], str]) -> None:
    """One fact per open refusal (``refused_reports``), on the node of the
    controller it refused."""

    for refusal in open_refusals():
        said = json.dumps([refusal.action, refusal.detail, refusal.refused_at.isoformat()])
        _on_controller(nodes, machine, refusal.controller_id, ((REFUSED_REPORT, said),))


@dataclass(frozen=True, slots=True)
class OpenRefusal:
    """A refusal as its controller's node carries it."""

    action: str
    detail: str
    refused_at: datetime | None


def refusals_on(node: TopologyNode) -> tuple[OpenRefusal, ...]:
    found = []
    for value in fact_values(node, REFUSED_REPORT):
        action, detail, at = json.loads(value)
        found.append(OpenRefusal(action, detail, moment(at)))
    return tuple(found)


def taken_at(node: TopologyNode, kind: str) -> datetime | None:
    """When the newest reading of ``kind`` on ``node`` was taken."""

    times = (
        moment(at) for value in fact_values(node, TAKEN) for named, _, at in (value.partition("|"),) if named == kind
    )
    return max((at for at in times if at is not None), default=None)


def too_old(estate: FindingEstate, taken: datetime | None) -> bool:
    """Whether a reading taken then is past the slowest the controller ever
    reads, so what it says no longer tells how things stand. A reading with
    no time is judged."""

    return taken is not None and passed(taken + slowest_sweep_interval(), now=estate.now)


def stated[R: FactRow](estate: FindingEstate, row: type[R]) -> Iterator[tuple[TopologyNode, tuple[R, ...]]]:
    """Each node that carries rows of one kind, with the rows, while its
    reading is young enough to judge."""

    for node in estate.nodes():
        values = fact_values(node, row.KEY)
        if values and not too_old(estate, taken_at(node, row.KIND)):
            yield node, tuple(row.of(value) for value in values)


@dataclass(frozen=True, slots=True)
class OnEachMachine[R: FactRow]:
    """A detector that says one thing per machine about the rows of its reading
    it picks: one finding, with each picked row's evidence and steps in turn,
    a command on that machine HQ cannot run."""

    rule: str
    row: type[R]
    picked: Callable[[R], bool]
    severity: Callable[[tuple[R, ...]], str]
    title: Callable[[TopologyNode, tuple[R, ...]], str]
    explanation: Callable[[tuple[R, ...]], str]
    evidence: Callable[[R], Iterable[tuple[str, str]]]
    steps: Callable[[TopologyNode, R], Iterable[OperatorStep]]

    def __call__(self, estate: FindingEstate) -> tuple[Finding, ...]:
        found = []
        for node, rows in stated(estate, self.row):
            picked = tuple(row for row in rows if self.picked(row))
            if not picked:
                continue
            found.append(
                Finding(
                    rule=self.rule,
                    subject=node.id,
                    scope=self.row.KIND,
                    title=self.title(node, picked),
                    severity=self.severity(picked),
                    explanation=self.explanation(picked),
                    evidence=tuple(item for row in picked for item in self.evidence(row)),
                    steps=tuple(step for row in picked for step in self.steps(node, row)),
                    no_help_reason=cannot_run_commands(node.label),
                )
            )
        return tuple(found)


@dataclass(frozen=True, slots=True)
class Unjudged:
    """A node's reading too old to judge: when it was taken and what it is
    said with, the refused report that kept it from being replaced included."""

    node: TopologyNode
    taken: datetime
    refusal: OpenRefusal | None

    @property
    def explanation(self) -> str:
        said = f"The controller last sent this reading {when(self.taken)}, too long ago to tell how things stand now."
        if self.refusal is None:
            return said
        return f"{said} HQ is refusing the report its readings arrive in: {ended(self.refusal.detail)}"

    @property
    def evidence(self) -> tuple[tuple[str, str], ...]:
        return (
            ("Last read", when(self.taken)),
            *((("Why it has not been read since", self.refusal.detail),) if self.refusal else ()),
        )

    def finding(self, rule: str, scope: str, title: str) -> Finding:
        """The reading's "not read" claim. Reading again is offered unless
        HQ refuses the report, which another reading would not get past."""

        return Finding(
            rule=rule,
            subject=self.node.id,
            scope=scope,
            title=title,
            severity="attention",
            explanation=self.explanation,
            evidence=self.evidence,
            remedies=() if self.refusal else (FindingRemedy(capability=CONTROLLER_REFRESH.name, target=""),),
        )


def unjudged(estate: FindingEstate, row: type[FactRow]) -> Iterator[Unjudged]:
    """Each node whose reading of ``row.KIND`` is too old to judge."""

    for node in estate.nodes():
        if not fact_values(node, row.KEY):
            continue
        taken = taken_at(node, row.KIND)
        if taken is not None and too_old(estate, taken):
            refusal = next((item for item in refusals_on(node) if item.action == READINGS_ACTION), None)
            yield Unjudged(node, taken, refusal)
