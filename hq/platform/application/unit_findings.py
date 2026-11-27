"""Findings about the systemd units a machine runs for HQ: failed, not
installed or enabled, not started, restarting, or unread.

A failed unit stays failed in its journal and nothing else notices. The rules
name no unit, so one added to ``deploy/systemd`` is covered once installed.
"""

import shlex
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils.text import capfirst, get_text_list

from hq.domains.control_plane.observations.host import UNIT_KIND, unescaped

from . import render_status_findings
from .action_links import ActionLink
from .finding_model import (
    Finding,
    FindingEstate,
    FindingRule,
    cannot_run_commands,
    matter_key,
)
from .finding_steps import ByHand, Why
from .moments import duration, moment, when
from .quoted import Quoted
from .reading_facts import OnEachMachine, ReadingRow, stated, unjudged
from .security import Capability
from .tone import Tone
from .topology_model import TopologyNode
from .ui import agreed, counted, ended

# ``LoadState`` when systemd holds the unit's configuration, and
# ``UnitFileState`` when the file is there and nothing starts it at boot.
LOADED = "loaded"
NOT_ENABLED = frozenset({"disabled", "masked", "masked-runtime", "invalid", "bad"})
# The unit types that start another unit on their own. A socket is active
# while it listens ("listening") or while its service holds the connection
# ("running"); inactive, nothing answers it.
STARTERS = (".timer", ".path", ".socket")
# The unit types that do their work only while active: each starter, and a
# mount, whose files are not there while it is inactive.
KEPT_ACTIVE = (*STARTERS, ".mount")
# The unit types that run when started and rest inactive between runs.
RUN_WHEN_STARTED = (".service",)
# ``test_unit_findings`` holds every unit type deploy/systemd ships to one of
# the two.
# How far behind its own next elapse a waiting timer may be when it is read.
# Above every RandomizedDelaySec shipped, which systemd adds after that instant.
OVERDUE_AFTER = timedelta(hours=2)
# How long a unit a timer started may still be starting when it is read. Above
# every TimeoutStartSec shipped, so systemd's own bound ends a slow run first.
STARTING_TOO_LONG = timedelta(hours=2)
# ``test_unit_findings`` holds both to the files under deploy/systemd.
# How often systemd must have restarted a service on its own for HQ to call it
# a loop. Above systemd's default StartLimitBurst of 5: a service restarting
# faster than its start limit allows is stopped and failed, ``unit-failed``'s
# to say, so a count past it is a loop systemd does not end. A start or
# ``systemctl reset-failed`` sets the count back to zero.
RESTARTING_FROM = 10
# How long a restarted service must have been active, when it was read, to
# have settled: longer than a loop's RestartSec and one run before it stops.
SETTLED_AFTER = timedelta(minutes=10)

# The host's own program, as a unit runs it.
HQ_HOST = "/usr/local/lib/severino-hq/deploy/bin/hq-host"

FAILED = "unit-failed"
NOT_INSTALLED = "unit-not-installed"

# Why the reading holds no state for a machine, as its item says it.
UNREAD_REASONS = {
    "unread": "HQ could not read them",
    "refused": "their answer could not be understood",
}


@dataclass(frozen=True, slots=True)
class UnitState(ReadingRow):
    """One unit as its record says it: words and instants."""

    KIND = UNIT_KIND

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
    # What the unit is, in its unit file's words; "" when systemd gave none.
    title: str = ""
    # How often systemd restarted it on its own, "" for none, and when it
    # last became active.
    restarts: str = ""
    active_at: str = ""
    # "yes" for a service HQ does not ship, read because systemd restarted it
    # or holds it as failed.
    foreign: str = ""
    # The units it cannot start without, and what HQ's own program last told
    # systemd of it, such as why its run failed.
    requires: tuple[str, ...] = ()
    status: str = ""

    @classmethod
    def record(cls, record: dict[str, Any], snapshot: Any) -> UnitState:
        return unit_state(record)

    @property
    def label(self) -> str:
        """The unit as a person reads it: its title, else its name."""

        return self.title or unescaped(self.unit)

    @property
    def named(self) -> str:
        """The unit in a sentence: its title quoted, since a unit file's title
        reads as a phrase of its own, else its name."""

        return f"“{self.title}”" if self.title else unescaped(self.unit)

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
    def kept_active(self) -> bool:
        return self.unit.endswith(KEPT_ACTIVE)

    @property
    def looping(self) -> bool:
        """Whether systemd keeps restarting the service and it has not stayed up.

        A failed one is ``unit-failed``'s, so one failure is one finding."""

        if int(self.restarts or 0) < RESTARTING_FROM or self.failed:
            return False
        if self.active == "active":
            return bool(self.active_at) and not self.behind(self.active_at, SETTLED_AFTER)
        return self.sub == "auto-restart" or self.active == "activating"

    @property
    def absent(self) -> str:
        """Why the unit cannot do its work, or "" when nothing says so."""

        if not self.installed:
            return "Not installed"
        if self.file_state in NOT_ENABLED:
            return "Switched off"
        if self.kept_active and self.active not in ("active", "failed"):
            return "Not started"
        return ""

    def behind(self, stamp: str, by: timedelta) -> bool:
        """Whether ``stamp`` was more than ``by`` before the unit was read."""

        # Judged as of the reading, never now: a reading too old to judge is
        # ``unit-state-unread``'s to say.
        then, read = moment(stamp), moment(self.read_at)
        return then is not None and read is not None and read - then > by


def unit_state(record: dict[str, Any]) -> UnitState:
    """One `host.unit` record as the rules read it."""

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
        title=said("title"),
        restarts=said("restarts"),
        active_at=said("active_at"),
        foreign="yes" if record.get("foreign") else "",
        requires=tuple(str(name) for name in record.get("requires") or ()),
        status=said("status"),
    )


def machine_units(estate: FindingEstate) -> Iterator[tuple[TopologyNode, tuple[UnitState, ...]]]:
    """Each machine's units, without the rows that say the reading failed."""

    for node, rows in stated(estate, UnitState):
        units = tuple(row for row in rows if row.unit)
        if units:
            yield node, units


# Who is told a worker on HQ's machine stopped reporting: whoever may read
# HQ. It is about no record or reading, so no subject or scope gates it.
WORKER_READ = (str(Capability.READ),)


def unit_causes(estate: FindingEstate, unit: str) -> tuple[str, ...]:
    """The findings a worker's silence follows from, on the machine whose
    units include ``unit``, the one that runs it: that unit failed, or it or
    what starts it is not set up."""

    causes = []
    for node, units in machine_units(estate):
        starts = tuple(each for each in units if unit in (each.unit, each.activates))
        if any(each.unit == unit and each.failed for each in starts):
            causes.append(matter_key(FAILED, node.id))
        if any(each.absent for each in starts):
            causes.append(matter_key(NOT_INSTALLED, node.id))
    return tuple(causes)


def stopped_reporting(
    estate: FindingEstate,
    seen_at: datetime,
    *,
    rule: str,
    unit: str,
    title: str,
    explanation: str,
    severity: Tone,
    primary: ActionLink,
    part: str = "",
    fixes: tuple[ByHand, ...] = (),
) -> Finding:
    """The one finding for a worker on HQ's machine, run by ``unit``, that
    stopped reporting: when it was last seen and how to read why, beneath its
    unit's own failure."""

    return Finding(
        rule=rule,
        subject="",
        part=part,
        read_capabilities=WORKER_READ,
        title=title,
        severity=severity,
        explanation=explanation,
        evidence=(("Last seen", when(seen_at, now=estate.now)),),
        fixes=(
            *fixes,
            ByHand("Read why it stopped", f"sudo journalctl -u {shlex.quote(unit)} -n 50 --no-pager", Why.NOT_GRANTED),
        ),
        primary=primary,
        follows=unit_causes(estate, unit),
    )


def _ended(unit: UnitState) -> str:
    """How the last run ended, in systemd's words."""

    result = unit.result or "unknown"
    return f"{result}, status {unit.main_status}" if unit.main_status else result


def _failed_when(machine: str, unit: UnitState, now: datetime) -> str:
    return when(unit.ended_at, now) if unit.ended_at else f"before {machine} last started"


def _failed(estate: FindingEstate) -> tuple[Finding, ...]:
    """A unit systemd holds as failed.

    A renderer whose own status says its run failed is ``render-failing``'s to
    say, with the class of the failure; the unit is left out here so one
    failure is one finding.
    """

    found = []
    said = render_status_findings.failed_units(estate)
    for node, units in machine_units(estate):
        failed = tuple(unit for unit in units if unit.failed and unit.unit not in said.get(node.id, ()))
        if not failed:
            continue
        # A unit a timer, path or socket starts runs again on its own; one nothing
        # starts again stays down until someone does.
        scheduled = {unit.activates for unit in units if unit.starter}
        found.append(
            Finding(
                rule=FAILED,
                subject=node.id,
                scope=UNIT_KIND,
                title=(
                    f"{failed[0].named} failed on {node.label}"
                    if len(failed) == 1
                    else f"{counted(len(failed), 'background job', 'background jobs')} failed on {node.label}"
                ),
                severity=Tone.ATTENTION if all(unit.unit in scheduled for unit in failed) else Tone.SERIOUS,
                # What HQ's own program said as the run failed, where one unit
                # failed and said it; each one's, beside it, where several did.
                explanation=(
                    _why(failed[0])
                    if len(failed) == 1 and failed[0].status
                    else agreed(len(failed), "It has not run since.", "None has run since.")
                ),
                evidence=tuple(
                    item
                    for unit in failed
                    for item in (
                        ("Job", unit.unit),
                        ("How it ended", _ended(unit)),
                        *((("Why", _why(unit)),) if unit.status and len(failed) > 1 else ()),
                        ("Failed", _failed_when(node.label, unit, estate.now)),
                    )
                ),
                fixes=tuple(
                    step
                    for unit in failed
                    for step in (
                        *ByHand.journal(node.label, f"See why {unit.named} failed", unit.unit),
                        *ByHand.on(
                            node.label,
                            f"Start {unit.named} again",
                            f"systemctl restart {shlex.quote(unit.unit)}",
                            Why.NOT_GRANTED,
                        ),
                    )
                ),
                no_help_reason=cannot_run_commands(node.label),
                # A job cannot start without a unit it requires, so its failure
                # follows from that unit not being set up.
                follows=(matter_key(NOT_INSTALLED, node.id),) if _requires_absent(failed, units) else (),
            )
        )
    return tuple(found)


def _why(unit: UnitState) -> Quoted:
    """Why the unit's last run failed, as HQ's own program told systemd."""

    return Quoted(ended(capfirst(unit.status)))


def _requires_absent(failed: tuple[UnitState, ...], units: tuple[UnitState, ...]) -> bool:
    """Whether a failed unit requires one of ``units`` that is not set up."""

    absent = {unit.unit for unit in units if unit.absent and not unit.foreign}
    return any(name in absent for unit in failed for name in unit.requires)


@dataclass(frozen=True, slots=True)
class Setup:
    """How a shipped unit in one of ``UnitState.absent``'s states is set up:
    the act its item's sentence names and the command its step runs, so the
    two never name different remedies."""

    sentence: str
    label: str
    command: Callable[[UnitState], str]


# Installing the release installs, enables and starts every unit it ships
# (``hq-host install``); a unit it installed that is off or idle needs only
# turning on or starting.
SETUP = {
    "Not installed": Setup(
        "Installing HQ's release sets {it} up.", "Install HQ's release", lambda _unit: f"{HQ_HOST} install"
    ),
    "Switched off": Setup(
        "Turning {it} on sets {it} up.",
        "Turn on {unit}",
        lambda unit: f"systemctl enable --now {shlex.quote(unit.unit)}",
    ),
    "Not started": Setup(
        "Starting {it} sets {it} up.", "Start {unit}", lambda unit: f"systemctl start {shlex.quote(unit.unit)}"
    ),
}


def _set_up(node: TopologyNode, absent: tuple[UnitState, ...]) -> tuple[str, tuple[ByHand, ...]]:
    """What sets ``absent`` up on ``node``: a sentence per state and each
    distinct command once, both from ``SETUP``."""

    states = {unit.absent: [each for each in absent if each.absent == unit.absent] for unit in absent}
    sentence = " ".join(
        SETUP[state].sentence.format(it=agreed(len(units), "it", "them")) for state, units in states.items()
    )
    steps: dict[str, ByHand] = {}
    for unit in absent:
        setup = SETUP[unit.absent]
        command = setup.command(unit)
        if command not in steps:
            steps[command] = ByHand.on(node.label, setup.label.format(unit=unit.named), command, Why.NOT_GRANTED)[0]
    return sentence, tuple(steps.values())


def _absent(estate: FindingEstate) -> tuple[Finding, ...]:
    """A shipped unit the machine does not have, has disabled, or has not
    started: a timer, path or socket that is not active starts nothing, and a
    mount that is not active holds nothing."""

    found = []
    for node, units in machine_units(estate):
        # A service another package installs is read only for its restarts
        # and failures; deploying HQ sets up none of it.
        absent = tuple(unit for unit in units if unit.absent and not unit.foreign)
        if not absent:
            continue
        sentence, steps = _set_up(node, absent)
        found.append(
            Finding(
                rule=NOT_INSTALLED,
                subject=node.id,
                scope=UNIT_KIND,
                title=(
                    f"{absent[0].named} is not set up on {node.label}"
                    if len(absent) == 1
                    else f"{counted(len(absent), 'background job', 'background jobs')} are not set up on {node.label}"
                ),
                severity=Tone.SERIOUS,
                explanation=sentence,
                evidence=tuple(item for unit in absent for item in (("Job", unit.unit), ("State", unit.absent))),
                fixes=steps,
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
        return f"{started.named} was skipped because a condition it needs was not met"
    if started.active == "activating" and started.behind(started.started_at, STARTING_TOO_LONG):
        return f"{started.named} had been starting for more than {duration(STARTING_TOO_LONG)}"
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
    for node, units in machine_units(estate):
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
            f"{timers[0].named} on {node.label} is not starting its job"
            if len(timers) == 1
            else f"{counted(len(timers), 'timer')} on {node.label} are not starting their jobs"
        ),
        severity=Tone.SERIOUS,
        # Why one is stalled, said as its reason; each one's, beside it, where several are.
        explanation=ended(stalled[0][1]) if len(stalled) == 1 else "None of their jobs is running on schedule.",
        evidence=tuple(
            item
            for timer, why in stalled
            for item in (
                ("Timer", timer.unit),
                *((("Why", why),) if len(stalled) > 1 else ()),
                ("Last ran", when(timer.last_trigger_at, now)),
            )
        ),
        fixes=tuple(
            step
            for timer in timers
            for step in (
                *ByHand.on(
                    node.label,
                    f"See when {timer.named} last ran and is next due",
                    f"systemctl list-timers {shlex.quote(timer.unit)}",
                    Why.NOT_GRANTED,
                ),
                *(
                    ByHand.journal(node.label, "See what the job it starts last did", timer.activates)
                    if timer.activates
                    else ()
                ),
            )
        ),
        no_help_reason=cannot_run_commands(node.label),
    )


# A service systemd keeps restarting on its own that does not stay up.
# systemd restarts a service with ``Restart=`` set for as long as its start
# limit allows, and a service slower to fail than that limit is restarted
# forever: it never reads as failed. The reading holds every such service on
# the machine, HQ's or not, so a loop in a unit nothing else watches is seen.
_restarting = OnEachMachine(
    rule="unit-restarting",
    row=UnitState,
    picked=lambda unit: bool(unit.unit) and unit.looping,
    service_unit=lambda unit: unit.unit,
    severity=lambda _looping: Tone.SERIOUS,
    title=lambda node, looping: (
        f"{looping[0].named} keeps restarting on {node.label}"
        if len(looping) == 1
        else f"{counted(len(looping), 'service keeps', 'services keep')} restarting on {node.label}"
    ),
    explanation=lambda looping: (
        _why(looping[0])
        if len(looping) == 1 and looping[0].status
        else agreed(
            len(looping), "It stops again each time systemd starts it.", "Each stops again when systemd starts it."
        )
    ),
    evidence=lambda unit: (
        ("Service", unit.unit),
        ("Restarted", f"{counted(int(unit.restarts), 'time')} since it was last started"),
        ("How it last ended", _ended(unit)),
    ),
    fixes=lambda node, unit: (
        *ByHand.journal(node.label, f"See why {unit.named} keeps stopping", unit.unit),
        *ByHand.on(
            node.label,
            "Once it is fixed, clear its restart count",
            f"systemctl reset-failed {shlex.quote(unit.unit)}",
            Why.NOT_GRANTED,
        ),
    ),
)


def _unread(estate: FindingEstate) -> tuple[Finding, ...]:
    """A machine whose unit state could not be read, or was read too long ago
    to say how its units stand now.

    HQ cannot say whether its units run, which is not the same as saying
    they do.
    """

    found = [
        aged.finding("unit-state-unread", UNIT_KIND, _cannot_check(aged.node)) for aged in unjudged(estate, UnitState)
    ]
    for node, rows in stated(estate, UnitState):
        reasons = tuple(dict.fromkeys(row.reason for row in rows if row.reason))
        if not reasons:
            continue
        found.append(
            Finding(
                rule="unit-state-unread",
                subject=node.id,
                scope=UNIT_KIND,
                title=_cannot_check(node),
                severity=Tone.ATTENTION,
                explanation=ended(
                    capfirst(
                        get_text_list(
                            [UNREAD_REASONS.get(reason, "they could not be read") for reason in reasons], "and"
                        )
                    )
                ),
                fixes=ByHand.on(
                    node.label,
                    "See whether systemd answers for HQ's jobs",
                    "systemctl list-units 'severino-hq-*' --all --no-pager",
                    Why.NOT_GRANTED,
                    ("If that works and this stays, deploy HQ.",),
                ),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


def _cannot_check(node: TopologyNode) -> str:
    return f"HQ cannot check the background jobs on {node.label}"


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        FAILED,
        "Background job failed",
        Tone.SERIOUS,
        _failed,
        gathered=True,
        operator_action="Read the job's log on the machine, fix what it names, then start it again.",
        no_help_reason=cannot_run_commands(),
        bars={
            Tone.SERIOUS: "a job nothing starts again on its own has failed",
            Tone.ATTENTION: "a scheduled job failed, which its timer runs again",
        },
    ),
    FindingRule(
        NOT_INSTALLED,
        "Background job not set up",
        Tone.SERIOUS,
        _absent,
        gathered=True,
        operator_action="Install HQ's release on the machine, or start the job there.",
        no_help_reason=cannot_run_commands(),
        bars={Tone.SERIOUS: "a job HQ needs is not installed or not turned on"},
    ),
    FindingRule(
        "timer-stalled",
        "Scheduled job not running",
        Tone.SERIOUS,
        _stalled,
        gathered=True,
        operator_action="Read the job's log on the machine, then start the job.",
        no_help_reason=cannot_run_commands(),
        bars={
            Tone.SERIOUS: (
                f"a timer is {duration(OVERDUE_AFTER)} overdue, "
                f"or its job has been starting for {duration(STARTING_TOO_LONG)}"
            )
        },
        clears=f"the timer runs within {duration(OVERDUE_AFTER)} of when it is due",
    ),
    FindingRule(
        "unit-restarting",
        "Service keeps restarting",
        Tone.SERIOUS,
        _restarting,
        gathered=True,
        operator_action="Read the service's log on the machine, fix what it names, then clear its restart count.",
        no_help_reason=cannot_run_commands(),
        bars={
            Tone.SERIOUS: (
                f"a service restarted {RESTARTING_FROM} times without staying up for {duration(SETTLED_AFTER)}"
            )
        },
        clears=f"the service stays up for {duration(SETTLED_AFTER)}, or its restart count is cleared",
    ),
    FindingRule(
        "unit-state-unread",
        "Background jobs not checked",
        Tone.ATTENTION,
        _unread,
        gathered=True,
        operator_action="Check that systemd answers on the controller's machine. If it does and this stays, deploy HQ.",
        no_help_reason=cannot_run_commands(),
        bars={Tone.ATTENTION: "HQ cannot read whether its background jobs run"},
    ),
)
