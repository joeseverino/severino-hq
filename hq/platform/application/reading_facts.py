"""What a controller's reading of its own machine says, about that machine's node.

Each stored record is one row about the controller's node, or the machine a
connection reaches (``placed``). A reading that could not be taken is a row
too, so what went quiet is said. Once out of date (``freshness``), ``stated``
withholds its rows from every rule and ``unjudged`` says why.
"""

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Self

if TYPE_CHECKING:
    from hq.domains.control_plane.models import RefusedReport

from .cadence import CONTROLLER_REFRESH
from .finding_model import Finding, FindingEstate, FindingRemedy, cannot_run_commands
from .finding_steps import Do, Run
from .freshness import LAST_READ, freshness
from .moments import when
from .references import Kind
from .tone import Tone
from .topology_model import TopologyNode, derived_id
from .ui import ended
lazy from .facts import snapshots_of
lazy from .refused_reports import READINGS_ACTION, open_refusals
lazy from .topology_estate import machine_nodes

# Why a reading holds no record, when the reading itself is what failed.
UNREAD = "unread"
REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class ReadingRow:
    """What one stored record of ``KIND`` says, as its rules read it.

    The subclass is a frozen dataclass whose fields all default, and
    ``record`` reads one stored record into it. A row of ``reason`` alone
    says the reading itself failed, and names nothing it reads.
    """

    KIND: ClassVar[str]

    reason: str = ""

    @classmethod
    def record(cls, record: dict[str, Any], snapshot: Any) -> Self:
        raise NotImplementedError

    @property
    def through(self) -> str:
        """The connection the row was read through, "" for its controller's own machine."""

        return ""

    @classmethod
    def rows(cls, snapshot: Any) -> Iterator[Self]:
        """What one stored reading says: a row per record, and the reading's
        own failure as a row of ``UNREAD`` or ``REFUSED``."""

        if not snapshot.reachable:
            yield cls(reason=UNREAD)
            return
        for record in snapshot.records or ():
            yield cls.record(record, snapshot)
        if snapshot.error:
            yield cls(reason=REFUSED)


def placed(
    nodes: Mapping[str, TopologyNode], machine: Callable[[Any], str], controller: str, through: str = ""
) -> TopologyNode:
    """The node what ``controller`` read stands on: for what it read
    ``through`` a connection, the machine that connection reaches, which an
    SSH transport's own name stands for; else the controller's node, or the
    machine node it folded into. Where none stands, a node of its own, so
    what it says is never dropped for want of somewhere to say it."""

    if through:
        return _reached(nodes, machine, through)
    node_id = derived_id(Kind.CONTROLLER, controller)
    if node_id not in nodes:
        node_id = machine(controller) or node_id
    return nodes.get(node_id) or TopologyNode(
        id=node_id, kind="controller", label=controller or "The controller", subtitle="Controller"
    )


def _reached(nodes: Mapping[str, TopologyNode], machine: Callable[[Any], str], ref: str) -> TopologyNode:
    node_id = machine(ref) or derived_id(Kind.TARGET, ref)
    return nodes.get(node_id) or TopologyNode(id=node_id, kind="target", label=ref, subtitle="Machine")


def stands(nodes: dict[str, TopologyNode], machine: Callable[[Any], str], controller: str, through: str = "") -> None:
    """Make sure the node ``placed`` picks stands, so a finding about it has a
    node to be read through."""

    node = placed(nodes, machine, controller, through)
    nodes.setdefault(node.id, node)


def add_readers(
    nodes: dict[str, TopologyNode], machine: Callable[[Any], str], rows: Iterable[type[ReadingRow]]
) -> None:
    """The node each stored row of ``rows``, and each open refusal, is about,
    where none stands yet."""

    for row in rows:
        for snapshot in snapshots_of(row.KIND):
            for item in row.rows(snapshot):
                stands(nodes, machine, snapshot.controller_id, item.through)
    for refusal in open_refusals():
        stands(nodes, machine, refusal.controller_id)


def by_node[T](estate: FindingEstate, items: Iterable[tuple[str, str, T]]) -> tuple[tuple[TopologyNode, list[T]], ...]:
    """Each ``(controller, through, item)`` on the node ``placed`` puts it on,
    by node in the projection's order."""

    nodes = {node.id: node for node in estate.nodes()}
    machine = machine_nodes()
    found: dict[str, tuple[TopologyNode, list[T]]] = {}
    for controller, through, item in items:
        node = placed(nodes, machine, controller, through)
        found.setdefault(node.id, (node, []))[1].append(item)
    order = {node_id: index for index, node_id in enumerate(nodes)}
    return tuple(found[node_id] for node_id in sorted(found, key=lambda node_id: order.get(node_id, len(order))))


def refusals(estate: FindingEstate) -> tuple[tuple[TopologyNode, tuple[RefusedReport, ...]], ...]:
    """Each open refusal (``refused_reports``), by the node of the controller it refused."""

    found = by_node(estate, ((refusal.controller_id, "", refusal) for refusal in open_refusals()))
    return tuple((node, tuple(items)) for node, items in found)


@dataclass(frozen=True, slots=True)
class Read[R: ReadingRow]:
    """The rows of one kind about one node, and when the newest reading of them was taken."""

    node: TopologyNode
    rows: tuple[R, ...]
    taken: datetime | None


def read[R: ReadingRow](estate: FindingEstate, row: type[R]) -> tuple[Read[R], ...]:
    """Each node the stored rows of ``row.KIND`` are about, in the projection's order."""

    found = by_node(
        estate,
        (
            (snapshot.controller_id, item.through, (item, snapshot.observed_at))
            for snapshot in snapshots_of(row.KIND)
            for item in row.rows(snapshot)
        ),
    )
    return tuple(
        Read(node, tuple(item for item, _at in items), max((at for _item, at in items if at), default=None))
        for node, items in found
    )


def fresh[R: ReadingRow](estate: FindingEstate, row: type[R]) -> Iterator[Read[R]]:
    """What ``read`` holds while its reading is young enough to judge."""

    for found in read(estate, row):
        if not freshness(row.KIND, found.taken, estate.now).stale:
            yield found


def stated[R: ReadingRow](estate: FindingEstate, row: type[R]) -> Iterator[tuple[TopologyNode, tuple[R, ...]]]:
    """Each node rows of one kind are about, with the rows, while its reading
    is young enough to judge."""

    for found in fresh(estate, row):
        yield found.node, found.rows


@dataclass(frozen=True, slots=True)
class OnEachMachine[R: ReadingRow]:
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
    steps: Callable[[TopologyNode, R], Iterable[Run | Do]]

    def __call__(self, estate: FindingEstate) -> tuple[Finding, ...]:
        found = []
        for reading in fresh(estate, self.row):
            node = reading.node
            picked = tuple(row for row in reading.rows if self.picked(row))
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
                    observed_at=reading.taken,
                    reads=self.row.KIND,
                )
            )
        return tuple(found)


@dataclass(frozen=True, slots=True)
class Unjudged:
    """A node's reading too old to judge: when it was taken and what it is
    said with, the refused report that kept it from being replaced included."""

    node: TopologyNode
    taken: datetime
    refusal: RefusedReport | None

    @property
    def explanation(self) -> str:
        said = f"The controller last sent this reading {when(self.taken)}, too long ago to tell how things stand now."
        if self.refusal is None:
            return said
        return f"{said} HQ is refusing the report its readings arrive in: {ended(self.refusal.detail)}"

    @property
    def evidence(self) -> tuple[tuple[str, str], ...]:
        return (
            (LAST_READ, when(self.taken)),
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
            severity=Tone.ATTENTION,
            explanation=self.explanation,
            evidence=self.evidence,
            remedies=() if self.refusal else (FindingRemedy(capability=CONTROLLER_REFRESH.name, target=""),),
        )


def unjudged(estate: FindingEstate, row: type[ReadingRow]) -> Iterator[Unjudged]:
    """Each node whose reading of ``row.KIND`` is too old to judge."""

    refused = {node.id: held for node, held in refusals(estate)}
    for found in read(estate, row):
        if found.taken is not None and freshness(row.KIND, found.taken, estate.now).stale:
            refusal = next((item for item in refused.get(found.node.id, ()) if item.action == READINGS_ACTION), None)
            yield Unjudged(found.node, found.taken, refusal)
