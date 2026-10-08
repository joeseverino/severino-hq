"""Findings about the systemd units a machine runs for HQ: a unit that failed,
a unit that is not installed or not enabled, a timer that is not starting its
unit, and a machine whose units nobody could read.

The launcher asks systemd about every unit this repository ships and the
controller reports the answer as the ``host.unit`` reading (``units_state`` in
``scripts/lib/systemd-units.sh``). A unit that fails stays failed in that
machine's journal and nothing else notices, so the reading is one fact per unit
on the node of the controller that took it, and the rules here read those
facts. They name no unit: one added to ``deploy/systemd`` is covered when it
is installed.

A unit's state is judged as of when it was read. The age of the reading itself
is the sweep's to say.
"""

import shlex
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from hq.domains.control_plane.observations.host import UNIT_KIND

from . import render_status_findings
from .finding_model import (
    Finding,
    FindingEstate,
    FindingRule,
    cannot_run_commands,
    journal_step,
    machine_step,
)
from .moments import duration
from .reading_facts import FactRow, ago, rows_of, state_on_controller, stated
from .timestamps import moment
from .topology_model import TopologyNode
from .ui import counted

UNIT_STATE = "unit-state"

# ``LoadState`` when systemd holds the unit's configuration, and
# ``UnitFileState`` when the file is there and nothing starts it at boot.
LOADED = "loaded"
NOT_ENABLED = frozenset({"disabled", "masked", "masked-runtime", "invalid", "bad"})
# The unit types that start work on their own and so must be active to do it.
STARTERS = (".timer", ".path")
# How far behind its own next elapse a waiting timer may be when it is read.
# Above every RandomizedDelaySec shipped, which systemd adds after that instant.
OVERDUE_AFTER = timedelta(hours=2)
# How long a unit a timer started may still be starting when it is read. Above
# every TimeoutStartSec shipped, so systemd's own bound ends a slow run first.
STARTING_TOO_LONG = timedelta(hours=2)
# ``test_unit_findings`` holds both to the files under deploy/systemd.

# Why the reading holds no state for a machine, as evidence says it.
UNREAD_REASONS = {
    "unread": "The controller could not read them",
    "refused": "The answer could not be understood",
}


@dataclass(frozen=True, slots=True)
class UnitState(FactRow):
    """One unit as its fact carries it: words and instants, in field order."""

    KEY = UNIT_STATE

    unit: str = ""
    load: str = ""
    file_state: str = ""
    active: str = ""
    sub: str = ""
    result: str = ""
    main_status: str = ""
    started_at: str = ""
    ended_at: str = ""
    condition: str = ""
    condition_at: str = ""
    last_trigger_at: str = ""
    next_elapse_at: str = ""
    activates: str = ""
    read_at: str = ""
    # Set when the reading itself failed; such a row names no unit.
    reason: str = ""

    @property
    def failed(self) -> bool:
        return self.active == "failed"

    @property
    def installed(self) -> bool:
        return self.load == LOADED

    @property
    def starter(self) -> bool:
        return self.unit.endswith(STARTERS)

    @property
    def absent(self) -> str:
        """Why the unit cannot do its work, or "" when nothing says so."""

        if not self.installed:
            return "Not installed"
        if self.file_state in NOT_ENABLED:
            return "Switched off"
        if self.starter and self.active not in ("active", "failed"):
            return "Not started"
        return ""

    def behind(self, stamp: str, by: timedelta) -> bool:
        """Whether ``stamp`` was more than ``by`` before the unit was read."""

        then, read = moment(stamp), moment(self.read_at)
        return then is not None and read is not None and read - then > by


def _unit(record: dict[str, Any]) -> UnitState:
    def said(name: str) -> str:
        return str(record.get(name, "") or "")

    return UnitState(
        unit=said("unit"),
        load=said("load"),
        file_state=said("file_state"),
        active=said("active"),
        sub=said("sub"),
        result=said("result"),
        main_status=said("main_status"),
        started_at=said("started_at"),
        ended_at=said("ended_at"),
        condition=said("condition"),
        condition_at=said("condition_at"),
        last_trigger_at=said("last_trigger_at"),
        next_elapse_at=said("next_elapse_at"),
        activates=said("activates"),
        read_at=said("read_at"),
    )


def add(nodes: dict[str, TopologyNode], machine: Callable[[Any], str]) -> None:
    """One fact per unit, on the node of the controller that read it."""

    state_on_controller(
        nodes,
        machine,
        UNIT_KIND,
        lambda snapshot: rows_of(snapshot, _unit, lambda reason: UnitState(reason=reason)),
    )


def _units(estate: FindingEstate) -> Iterator[tuple[TopologyNode, tuple[UnitState, ...]]]:
    """Each machine's units, without the rows that say the reading failed."""

    for node, rows in stated(estate, UnitState):
        units = tuple(row for row in rows if row.unit)
        if units:
            yield node, units


def _ended(unit: UnitState) -> str:
    """How the last run ended, in systemd's words."""

    result = unit.result or "unknown"
    return f"{result}, status {unit.main_status}" if unit.main_status else result


def _failed_when(machine: str, unit: UnitState, now: datetime) -> str:
    return ago(unit.ended_at, now) if unit.ended_at else f"before {machine} last started"


def _failed_since(machine: str, failed: tuple[UnitState, ...], now: datetime) -> str:
    if len(failed) > 1:
        return "None of them has run successfully since it failed."
    return f"It failed {_failed_when(machine, failed[0], now)} and has not run successfully since."


def _failed(estate: FindingEstate) -> tuple[Finding, ...]:
    """A unit systemd holds as failed.

    A renderer whose own status says its run failed is ``render-failing``'s to
    say, with the class of the failure; the unit is left out here so one
    failure is one finding.
    """

    found = []
    for node, units in _units(estate):
        said = render_status_findings.failed_units(node)
        failed = tuple(unit for unit in units if unit.failed and unit.unit not in said)
        if not failed:
            continue
        found.append(
            Finding(
                rule="unit-failed",
                subject=node.id,
                scope=UNIT_KIND,
                title=(
                    f"{failed[0].unit} failed on {node.label}"
                    if len(failed) == 1
                    else f"{counted(len(failed), 'background job', 'background jobs')} failed on {node.label}"
                ),
                severity="serious",
                explanation=_failed_since(node.label, failed, estate.now),
                evidence=tuple(
                    item
                    for unit in failed
                    for item in (
                        ("Job", unit.unit),
                        ("How it ended", _ended(unit)),
                        ("Failed", _failed_when(node.label, unit, estate.now)),
                    )
                ),
                steps=tuple(
                    step
                    for unit in failed
                    for step in (
                        *journal_step(f"See why {unit.unit} failed", node.label, unit.unit),
                        *machine_step(
                            f"Start {unit.unit} again",
                            node.label,
                            f"systemctl restart {shlex.quote(unit.unit)}",
                        ),
                    )
                ),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


def _absent(estate: FindingEstate) -> tuple[Finding, ...]:
    """A shipped unit the machine does not have, has disabled, or has not
    started: a timer or path that is not active starts nothing."""

    found = []
    for node, units in _units(estate):
        absent = tuple(unit for unit in units if unit.absent)
        if not absent:
            continue
        found.append(
            Finding(
                rule="unit-not-installed",
                subject=node.id,
                scope=UNIT_KIND,
                title=(
                    f"{absent[0].unit} is not set up on {node.label}"
                    if len(absent) == 1
                    else f"{counted(len(absent), 'background job', 'background jobs')} are not set up on {node.label}"
                ),
                severity="serious",
                explanation=(
                    "So its work is not happening. Deploying HQ sets it up."
                    if len(absent) == 1
                    else "So their work is not happening. Deploying HQ sets them up."
                ),
                evidence=tuple(item for unit in absent for item in (("Job", unit.unit), ("State", unit.absent))),
                steps=tuple(
                    step
                    for unit in absent
                    if unit.installed and unit.starter
                    for step in machine_step(
                        f"Turn on {unit.unit}",
                        node.label,
                        f"systemctl enable --now {shlex.quote(unit.unit)}",
                    )
                ),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


def _stall(timer: UnitState, started: UnitState | None) -> str:
    """Why an active timer is not starting its unit, or ""."""

    if timer.sub == "elapsed":
        return "It has run out and nothing more is scheduled"
    if timer.sub == "waiting" and timer.behind(timer.next_elapse_at, OVERDUE_AFTER):
        return f"It was more than {duration(OVERDUE_AFTER)} overdue when HQ last read it"
    if started is None:
        return ""
    # systemd answers "no" for a unit it has not started since boot, with no
    # time: only a condition it checked was not met.
    if started.condition == "no" and started.condition_at:
        return f"{started.unit} was skipped because a condition it needs was not met"
    if started.active == "activating" and started.behind(started.started_at, STARTING_TOO_LONG):
        return f"{started.unit} had been starting for more than {duration(STARTING_TOO_LONG)}"
    return ""


def _stalled(estate: FindingEstate) -> tuple[Finding, ...]:
    """A timer that is active and whose unit is still not being run.

    systemd reports none of these as a failure: a timer with nothing left
    scheduled, one behind its own next elapse, a start skipped because the
    unit's condition does not hold, and a run that never ends, which keeps the
    timer from firing again. A timer that is not active is
    ``unit-not-installed``'s to say, and a unit that failed is ``unit-failed``'s.
    """

    found = []
    for node, units in _units(estate):
        by_name = {unit.unit: unit for unit in units}
        stalled = tuple(
            (timer, why)
            for timer in units
            if timer.unit.endswith(".timer")
            and timer.active == "active"
            and (why := _stall(timer, by_name.get(timer.activates)))
        )
        if not stalled:
            continue
        timers = tuple(timer for timer, _why in stalled)
        found.append(_stalled_finding(node, stalled, timers, estate.now))
    return tuple(found)


def _stalled_finding(
    node: TopologyNode,
    stalled: tuple[tuple[UnitState, str], ...],
    timers: tuple[UnitState, ...],
    now: datetime,
) -> Finding:
    return Finding(
        rule="timer-stalled",
        subject=node.id,
        scope=UNIT_KIND,
        title=(
            f"{timers[0].unit} on {node.label} is on but its job is not running"
            if len(timers) == 1
            else f"{counted(len(timers), 'timer')} on {node.label} are on but their jobs are not running"
        ),
        severity="serious",
        explanation=(
            f"{stalled[0][1]}."
            if len(stalled) == 1
            else "Each timer is on and the job it starts is not running on schedule."
        ),
        evidence=tuple(
            item
            for timer, why in stalled
            for item in (
                ("Timer", timer.unit),
                ("Why", why),
                ("Last ran", ago(timer.last_trigger_at, now)),
            )
        ),
        steps=tuple(
            step
            for timer in timers
            for step in (
                *machine_step(
                    f"See when {timer.unit} last ran and is next due",
                    node.label,
                    f"systemctl list-timers {shlex.quote(timer.unit)}",
                ),
                *(
                    journal_step(f"See what {timer.activates} last did", node.label, timer.activates)
                    if timer.activates
                    else ()
                ),
            )
        ),
        no_help_reason=cannot_run_commands(node.label),
    )


def _unread(estate: FindingEstate) -> tuple[Finding, ...]:
    """A machine whose unit state could not be read.

    HQ cannot say whether its units run, which is not the same as saying
    they do.
    """

    found = []
    for node, rows in stated(estate, UnitState):
        reasons = tuple(dict.fromkeys(row.reason for row in rows if row.reason))
        if not reasons:
            continue
        found.append(
            Finding(
                rule="unit-state-unread",
                subject=node.id,
                scope=UNIT_KIND,
                title=f"HQ cannot check the background jobs on {node.label}",
                severity="attention",
                explanation="If one of them failed, HQ would not know.",
                evidence=tuple(("Why", UNREAD_REASONS.get(reason, "Could not be read")) for reason in reasons),
                steps=machine_step(
                    "See whether systemd answers for HQ's jobs",
                    node.label,
                    "systemctl list-units 'severino-hq-*' --all --no-pager",
                    ("If that works and this stays, deploy HQ.",),
                ),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "unit-failed",
        "A background job failed",
        "serious",
        _failed,
        operator_action=("On the machine, read the job's log for why it failed, fix that, then start it again."),
        no_help_reason=cannot_run_commands(),
    ),
    FindingRule(
        "unit-not-installed",
        "A background job is not set up",
        "serious",
        _absent,
        operator_action=("Deploy HQ, which sets up every background job, or turn the job on on the machine."),
        no_help_reason=cannot_run_commands(),
    ),
    FindingRule(
        "timer-stalled",
        "A scheduled job is not running",
        "serious",
        _stalled,
        operator_action=("On the machine, see when the timer last ran and read its job's log, then start the job."),
        no_help_reason=cannot_run_commands(),
    ),
    FindingRule(
        "unit-state-unread",
        "Cannot check background jobs",
        "attention",
        _unread,
        operator_action=(
            "Check that systemd answers on the controller's machine. If it does and this stays, deploy HQ."
        ),
        no_help_reason=cannot_run_commands(),
    ),
)
