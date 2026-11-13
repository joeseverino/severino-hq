"""One pass of the automation worker: claim a run, hold its lease while it runs,
and report how it ended.

The runner is handed in, so a test runs the whole pass with a fake: ``ansible``
(``hq_runner.ansible``) runs a playbook through ansible-runner, and anything
with its signature stands in for it. Whatever runs, the worker decides when it
stops: when HQ answers a renewal with stop (the asker is held, or the run was
cancelled), when a renewal fails so the lease cannot be held, or when the
procedure's timeout passes.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import tracing
from .bridge import BridgeError
from .claim import CAPABILITY, Claimed, Procedure, Refused, Run, bind, claimed
from .events import Event, bounded, from_ansible, learned, strike
from .journal import Journal

# The lease a claim asks for, and how often a running run renews it: three
# renewals fit in one lease, so one lost renewal never lets it lapse.
LEASE_SECONDS = 120
RENEW_EVERY_SECONDS = 40

UNKNOWN = (
    "The worker stopped during this run before it reported, so whether it changed the machines is unknown. "
    "It is not run again."
)


class Bridge(Protocol):
    def claim(self, controller_id: str, *, capability: str, lease_seconds: int, attempt: str) -> dict[str, Any]: ...

    def renew(self, controller_id: str, operation: str, *, lease_seconds: int) -> dict[str, Any]: ...

    def report(self, controller_id: str, operation: str, report: Mapping[str, Any]) -> dict[str, Any]: ...


class Runner(Protocol):
    """Runs one bound run, passing each Ansible event to ``on_event`` and asking
    ``should_stop`` as it goes; answers ansible-runner's final status
    (``successful``, ``failed``, ``timeout``, ``canceled``)."""

    def __call__(
        self, run: Run, *, on_event: Callable[[Mapping[str, Any]], None], should_stop: Callable[[], bool]
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class Outcome:
    """How one pass ended: the operation it reported on and what it said."""

    operation: str
    report: dict[str, Any]


class _Hold:
    """The run's lease, renewed as it runs, and why it was told to stop."""

    def __init__(
        self, bridge: Bridge, controller_id: str, run: Run, clock: Callable[[], float], secrets: frozenset[str]
    ) -> None:
        self._bridge = bridge
        self._controller_id = controller_id
        self._run = run
        self._clock = clock
        self._secrets = secrets
        start = clock()
        self._renewed = start
        self._deadline = start + run.timeout_seconds
        self.stopped: tuple[str, str] | None = None
        # The run's span, which ansible-runner's thread asks this on.
        self.span: Any = None

    def should_stop(self) -> bool:
        if self.stopped is not None:
            return True
        now = self._clock()
        if now >= self._deadline:
            self.stopped = ("TimedOut", f"It ran past its procedure's {self._run.timeout_seconds} second timeout.")
        elif now - self._renewed >= RENEW_EVERY_SECONDS:
            self._renew(now)
        return self.stopped is not None

    def _renew(self, now: float) -> None:
        try:
            answer = self._bridge.renew(self._controller_id, self._run.operation, lease_seconds=LEASE_SECONDS)
        except BridgeError as exc:
            # A lease that cannot be held lapses, and HQ then treats the run
            # as unknown; stopping now keeps what it did as small as it can be.
            self.stopped = ("LeaseLost", strike(f"Its lease could not be renewed: {exc}", self._secrets))
            return
        self._renewed = now
        tracing.event(self.span, "lease renewed")
        if answer.get("continue") is not True:
            reason = answer.get("reason")
            if reason == "Cancelled":
                self.stopped = ("Cancelled", "It was cancelled in HQ.")
            else:
                self.stopped = ("Held", "HQ asked it to stop: whoever asked for it is paused or blocked.")


def _condition(reason: str, message: str) -> list[dict[str, Any]]:
    return [{"type": "Degraded", "status": True, "reason": reason, "message": message}]


def _report(
    generation: int, *, success: bool, message: str, reason: str = "", events: Sequence[Event] = ()
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "success": success,
        "observed_generation": generation,
        "message": message,
        "events": [event.as_report() for event in events],
    }
    if not success:
        report["conditions"] = _condition(reason, message)
    return report


_ENDED = {
    "successful": (True, "", "It ran to the end on every machine."),
    "failed": (False, "Failed", "A task failed or a machine could not be reached."),
}


def _finish(run: Run, status: str, hold: _Hold, events: list[Event]) -> dict[str, Any]:
    kept, left_out = bounded(events)
    if hold.stopped is not None:
        reason, message = hold.stopped
        success = False
    else:
        success, reason, message = _ENDED.get(status, (False, "Failed", f"ansible-runner ended it as {status}."))
    if left_out:
        message += f" {left_out} task outcomes that were fine or skipped are not listed."
    return _report(run.generation, success=success, message=message, reason=reason, events=kept)


def run_claimed(
    bridge: Bridge, controller_id: str, run: Run, runner: Runner, clock: Callable[[], float]
) -> dict[str, Any]:
    """Run ``run`` under its held lease and answer the report it ends with."""

    secrets = learned(run.parameters)
    hold = _Hold(bridge, controller_id, run, clock, secrets)
    events: list[Event] = []

    def on_event(event: Mapping[str, Any]) -> None:
        found = from_ansible(event, secrets)
        if found is not None:
            events.append(found)
            # The struck event's host, task and outcome; never its message.
            tracing.event(hold.span, found.outcome, **{"ansible.host": found.host, "ansible.task": found.task})

    with tracing.span("ansible-runner", **{"hq.procedure": run.procedure_id, "hq.hosts": len(run.hosts)}) as ran:
        hold.span = ran
        status = runner(run, on_event=on_event, should_stop=hold.should_stop)
        tracing.note(**{"ansible.status": status})
    report = _finish(run, status, hold, events)
    if hold.stopped is not None:
        tracing.note(**{"hq.stopped": hold.stopped[0]})
    return report


def work_once(
    bridge: Bridge,
    *,
    controller_id: str,
    installed: Mapping[str, Procedure],
    journal: Journal,
    runner: Runner,
    clock: Callable[[], float] = time.monotonic,
) -> Outcome | None:
    """Claim one run and see it through to its report; None when nothing waits."""

    with tracing.span("automation run", **{"hq.capability": CAPABILITY}):
        outcome = _pass(
            bridge, controller_id=controller_id, installed=installed, journal=journal, runner=runner, clock=clock
        )
        if outcome is not None:
            tracing.note(**{"hq.operation.id": outcome.operation, "hq.ok": outcome.report["success"] is True})
        return outcome


def _pass(
    bridge: Bridge,
    *,
    controller_id: str,
    installed: Mapping[str, Procedure],
    journal: Journal,
    runner: Runner,
    clock: Callable[[], float],
) -> Outcome | None:
    entry = journal.begin()
    pending = bridge.claim(controller_id, capability=CAPABILITY, lease_seconds=LEASE_SECONDS, attempt=entry.attempt)
    try:
        claim: Claimed | None = claimed(pending)
    except Refused:
        journal.clear()
        raise
    if claim is None:
        journal.clear()
        return None
    if entry.operation == claim.operation:
        report = entry.report or _report(claim.generation, success=False, message=UNKNOWN, reason="OutcomeUnknown")
    else:
        try:
            run = bind(claim, installed)
        except Refused as refused:
            report = _report(claim.generation, success=False, message=refused.message, reason=refused.reason)
        else:
            entry = journal.started(entry, claim.operation)
            report = run_claimed(bridge, controller_id, run, runner, clock)
            entry = journal.finished(entry, report)
    bridge.report(controller_id, claim.operation, report)
    journal.clear()
    return Outcome(claim.operation, report)
