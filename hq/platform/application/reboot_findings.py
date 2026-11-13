"""Findings about a machine that waits on a restart, or has one scheduled.

An update that waits on a restart runs the code it replaced until the machine
restarts, and the restart takes down everything on the machine while it
happens, so this says both before either surprises anyone. It reads the
``host.reboot`` reading's one fact per machine (docs/Derived-Facts.md says
how it is taken).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hq.domains.control_plane.observations.host import REBOOT_KIND

from .finding_model import FindingRule, OperatorStep, cannot_run_commands, machine_step
from .moments import when
from .reading_facts import FactRow, OnEachMachine, rows_of, state_on_controller
from .timestamps import moment
from .topology_model import TopologyNode

REBOOT_STATE = "reboot-state"

# What logind's scheduled action does, as a sentence says it.
_ACTIONS = {"reboot": "restart", "poweroff": "power off", "halt": "halt"}


@dataclass(frozen=True, slots=True)
class RebootState(FactRow):
    """A machine's restart state as its fact carries it, in field order."""

    KEY = REBOOT_STATE
    KIND = REBOOT_KIND

    pending: str = ""
    since: str = ""
    # The packages that wait on it, space-separated.
    packages: str = ""
    scheduled_action: str = ""
    scheduled_at: str = ""
    read_at: str = ""
    # Set when the reading itself failed; such a row says nothing of the machine.
    reason: str = ""

    @property
    def waiting(self) -> bool:
        return self.pending == "true"

    @property
    def scheduled(self) -> str:
        """When logind will act, as HQ writes a moment; "" when nothing is scheduled."""

        at = moment(self.scheduled_at) if self.scheduled_action else None
        return when(at) if at else ""

    @property
    def action(self) -> str:
        return _ACTIONS.get(self.scheduled_action, self.scheduled_action)


def reboot_state(record: dict[str, Any]) -> RebootState:
    """One ``host.reboot`` record as the rule reads it."""

    return RebootState(
        pending="true" if record.get("pending") else "",
        since=str(record.get("since", "") or ""),
        packages=" ".join(str(name) for name in record.get("packages") or ()),
        scheduled_action=str(record.get("scheduled_action", "") or ""),
        scheduled_at=str(record.get("scheduled_at", "") or ""),
        read_at=str(record.get("read_at", "") or ""),
    )


def add(nodes: dict[str, TopologyNode], machine: Callable[[Any], str]) -> None:
    """The machine's restart state, on the node of the controller that read it."""

    state_on_controller(
        nodes,
        machine,
        RebootState,
        lambda snapshot: rows_of(snapshot, reboot_state, lambda reason: RebootState(reason=reason)),
    )


def _title(node: TopologyNode, rows: tuple[RebootState, ...]) -> str:
    (row, *_) = rows
    if not row.waiting:
        return f"{node.label} is scheduled to {row.action} at {row.scheduled}"
    if row.scheduled:
        return f"{node.label} waits on a restart, scheduled for {row.scheduled}"
    return f"{node.label} waits on a restart"


def _explanation(rows: tuple[RebootState, ...]) -> str:
    (row, *_) = rows
    waiting = (
        "An update installed on the machine takes effect only once it restarts; until then it runs the code the "
        "update replaced."
    )
    if not row.scheduled:
        return waiting
    scheduled = f"systemd will {row.action} it at that time, and everything on it stops until it is back."
    return f"{waiting} {scheduled}" if row.waiting else scheduled


def _evidence(row: RebootState) -> tuple[tuple[str, str], ...]:
    since = moment(row.since)
    return (
        *((("Waiting since", when(since)),) if row.waiting and since else ()),
        *((("Packages", ", ".join(row.packages.split())),) if row.packages else ()),
        *((("Scheduled", f"To {row.action} at {row.scheduled}"),) if row.scheduled else ()),
    )


def _steps(node: TopologyNode, row: RebootState) -> tuple[OperatorStep, ...]:
    waits = (
        machine_step("See what waits on the restart", node.label, "cat /run/reboot-required.pkgs")
        if row.waiting
        else ()
    )
    if row.scheduled:
        return (
            *waits,
            *machine_step("See what is scheduled", node.label, "shutdown --show", ("shutdown -c cancels it.",)),
        )
    return (
        *waits,
        *machine_step(
            "Schedule the restart for a quiet hour",
            node.label,
            "shutdown -r 03:00",
            ("HQ reads the time back and shows it here.",),
        ),
    )


# A machine that waits on a restart, or has one scheduled, when it was read.
_waiting = OnEachMachine(
    rule="reboot-pending",
    row=RebootState,
    picked=lambda row: row.waiting or bool(row.scheduled),
    severity=lambda _rows: "attention",
    title=_title,
    explanation=_explanation,
    evidence=_evidence,
    steps=_steps,
)


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "reboot-pending",
        "Waiting on a restart",
        "attention",
        _waiting,
        operator_action="On the machine, read what waits on the restart, then schedule it for a time that suits.",
        no_help_reason=cannot_run_commands(),
    ),
)
