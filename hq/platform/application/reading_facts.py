"""Facts a controller's reading of its own machine puts on that machine's node.

A reading such as ``host.render_status`` or ``host.unit`` is taken by a
controller of the machine it runs on. Each record becomes one fact on the node
of that controller, a row of words and instants joined in field order, and
the rules read the rows back. A reading that could not be taken is a row too,
so what went quiet is said and not dropped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import fields, replace
from datetime import datetime
from typing import Any, ClassVar, Self

from .derivations import since
from .finding_model import FindingEstate, fact_values
from .moments import duration
from .timestamps import moment
from .topology_model import TopologyNode, derived_id

# Why a reading holds no record, when the reading itself is what failed.
UNREAD = "unread"
REFUSED = "refused"


class FactRow:
    """A frozen dataclass of strings carried whole as one fact.

    The subclass is a ``@dataclass(frozen=True)`` whose fields all default to
    ``""`` and hold no ``|``; ``KEY`` is the fact it is stored under.
    """

    KEY: ClassVar[str]

    @property
    def fact(self) -> tuple[str, str]:
        return self.KEY, "|".join(getattr(self, field.name) for field in fields(self))

    @classmethod
    def of(cls, value: str) -> Self:
        names = [field.name for field in fields(cls)]
        parts = (value.split("|") + [""] * len(names))[: len(names)]
        return cls(**dict(zip(names, parts)))


def rows_of(snapshot: Any, row: Callable[[dict[str, Any]], FactRow], unread: Callable[[str], FactRow]) -> Iterator[FactRow]:
    """What one stored reading says: a row per record, and the reading's own
    failure as a row ``unread`` makes from ``UNREAD`` or ``REFUSED``."""

    if not snapshot.reachable:
        yield unread(UNREAD)
        return
    for record in snapshot.records or ():
        yield row(record)
    if snapshot.error:
        yield unread(REFUSED)


def state_on_controller(
    nodes: dict[str, TopologyNode],
    machine: Callable[[Any], str],
    kind: str,
    rows: Callable[[Any], Iterable[FactRow]],
) -> None:
    """One fact per row of ``kind``, on the node of the controller that read it.

    ``machine`` names the machine node a controller folded into. A reading
    whose controller no node stands for gets a node of its own, so what it
    says is never dropped for want of somewhere to say it.
    """

    from .facts import snapshots_of

    for snapshot in snapshots_of(kind):
        found = tuple(row.fact for row in rows(snapshot))
        if not found:
            continue
        controller = str(getattr(snapshot, "controller_id", "") or "")
        node_id = derived_id("controller", controller)
        if node_id not in nodes:
            node_id = machine(controller) or node_id
        node = nodes.get(node_id) or TopologyNode(
            id=node_id, kind="controller", label=controller or "The controller", subtitle="Controller"
        )
        nodes[node_id] = replace(node, facts=node.facts + found)


def stated[R: FactRow](estate: FindingEstate, row: type[R]) -> Iterator[tuple[TopologyNode, tuple[R, ...]]]:
    """Each node that carries rows of one kind, with the rows."""

    for node in estate.nodes():
        values = fact_values(node, row.KEY)
        if values:
            yield node, tuple(row.of(value) for value in values)


def ago(stamp: str, now: datetime) -> str:
    """An instant as an age, in words that hold until the age reads differently."""

    when = moment(stamp)
    return f"{duration(since(when, now=now))} ago" if when is not None else "never"
