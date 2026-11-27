"""Findings about a machine that waits on a restart, or has one scheduled.

A waiting update runs the old code until a restart, and the restart takes down
everything on the machine, so this says both before either surprises anyone
(docs/Derived-Facts.md says how they are read).
"""

from dataclasses import dataclass, replace
from typing import Any

from hq.domains.control_plane.observations.host import EDGE_REBOOT_KIND, REBOOT_KIND

from .finding_model import Finding, FindingEstate, FindingRule, cannot_run_commands
from .finding_steps import ByHand, Why
from .moments import moment, since_when, when_at
from .reading_facts import OnEachMachine, ReadingRow
from .tone import Tone
from .topology_model import TopologyNode

# What logind's scheduled action does, as a sentence says it.
_ACTIONS = {"reboot": "restart", "poweroff": "power off", "halt": "halt"}


@dataclass(frozen=True, slots=True)
class RebootState(ReadingRow):
    """A machine's restart state as its record says it."""

    KIND = REBOOT_KIND

    pending: str = ""
    since: str = ""
    # The packages that wait on it, space-separated.
    packages: str = ""
    scheduled_action: str = ""
    scheduled_at: str = ""
    read_at: str = ""

    @classmethod
    def record(cls, record: dict[str, Any], snapshot: Any) -> RebootState:
        return reboot_state(record)

    @property
    def waiting(self) -> bool:
        return self.pending == "true"

    @property
    def scheduled(self) -> str:
        """When logind will act, as HQ writes a moment; "" when nothing is scheduled."""

        at = moment(self.scheduled_at) if self.scheduled_action else None
        return when_at(at) if at else ""

    @property
    def action(self) -> str:
        return _ACTIONS.get(self.scheduled_action, self.scheduled_action)


@dataclass(frozen=True, slots=True)
class EdgeRebootState(RebootState):
    """An edge's restart state, with the connection its receiver answered through."""

    KIND = EDGE_REBOOT_KIND

    connection_ref: str = ""

    @classmethod
    def record(cls, record: dict[str, Any], snapshot: Any) -> EdgeRebootState:
        return edge_reboot_state(record)

    @property
    def through(self) -> str:
        return self.connection_ref


def _said(record: dict[str, Any]) -> dict[str, str]:
    """What a restart record says, as a row's fields."""

    return {
        "pending": "true" if record.get("pending") else "",
        "since": str(record.get("since", "") or ""),
        "packages": " ".join(str(name) for name in record.get("packages") or ()),
        "scheduled_action": str(record.get("scheduled_action", "") or ""),
        "scheduled_at": str(record.get("scheduled_at", "") or ""),
        "read_at": str(record.get("read_at", "") or ""),
    }


def reboot_state(record: dict[str, Any]) -> RebootState:
    """One ``host.reboot`` record as the rule reads it."""

    return RebootState(**_said(record))


def edge_reboot_state(record: dict[str, Any]) -> EdgeRebootState:
    """One ``host.edge_reboot`` record as the rule reads it."""

    return EdgeRebootState(**_said(record), connection_ref=str(record.get("connection_ref", "") or ""))


def _title(node: TopologyNode, rows: tuple[RebootState, ...]) -> str:
    (row, *_) = rows
    if not row.waiting:
        return f"{node.label} is scheduled to {row.action} at {row.scheduled}"
    if row.scheduled:
        return f"{node.label} waits on a restart, scheduled for {row.scheduled}"
    return f"{node.label} waits on a restart"


def _explanation(rows: tuple[RebootState, ...]) -> str:
    (row, *_) = rows
    return (
        "Everything on it stops until it is back."
        if row.scheduled
        else "What was updated runs its old version until it restarts."
    )


def _evidence(row: RebootState) -> tuple[tuple[str, str], ...]:
    since = moment(row.since)
    return (
        *((("Waiting", since_when(since)),) if row.waiting and since else ()),
        *((("Packages", ", ".join(row.packages.split())),) if row.packages else ()),
        *((("Scheduled", f"To {row.action} at {row.scheduled}"),) if row.scheduled else ()),
    )


def _steps(node: TopologyNode, row: RebootState) -> tuple[ByHand, ...]:
    # What waits on it and when it is due are its evidence already.
    if row.scheduled:
        return ByHand.on(node.label, "Cancel it where it is not wanted", "shutdown -c", Why.NOT_GRANTED)
    return ByHand.on(
        node.label,
        "Schedule the restart for a quiet hour",
        "shutdown -r 03:00",
        Why.NOT_GRANTED,
        ("HQ reads the time back and shows it here.",),
    )


# A machine that waits on a restart, or has one scheduled, when it was read.
_waiting = OnEachMachine(
    rule="reboot-pending",
    row=RebootState,
    picked=lambda row: row.waiting or bool(row.scheduled),
    severity=lambda _rows: Tone.ATTENTION,
    title=_title,
    explanation=_explanation,
    evidence=_evidence,
    fixes=_steps,
)
# The same, of each edge.
_edge_waiting = replace(_waiting, row=EdgeRebootState)


def _restarts(estate: FindingEstate) -> tuple[Finding, ...]:
    """One finding per machine. A controller can run on an edge of its own,
    so one machine can hold both readings: what it says comes from the newer,
    under the controller's reading's scope, so its identity holds while the
    two are read in turn."""

    kept: dict[str, Finding] = {}
    for finding in (*_waiting(estate), *_edge_waiting(estate)):
        held = kept.get(finding.subject)
        if held is None:
            kept[finding.subject] = finding
            continue
        at, held_at = finding.observed_at, held.observed_at
        newer = finding if at is not None and (held_at is None or at > held_at) else held
        kept[finding.subject] = replace(newer, scope=REBOOT_KIND)
    return tuple(kept.values())


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "reboot-pending",
        "Waiting on a restart",
        Tone.ATTENTION,
        _restarts,
        gathered=True,
        operator_action="Schedule the restart for a quiet hour.",
        no_help_reason=cannot_run_commands(),
        bars={Tone.ATTENTION: "an update waits on a restart to take effect"},
    ),
)
