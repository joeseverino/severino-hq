"""One pass of the automation worker: claim a run or a scan, hold a run's lease
while it runs, and report how it ended.

The runner and the scan are handed in, so a test runs the whole pass with
fakes: ``ansible`` (``hq_runner.ansible``) runs a playbook through
ansible-runner and ``catalog`` reads the source, and anything with their
signatures stands in for them. Whatever runs, the worker decides when it
stops: when HQ answers a renewal with stop (the asker is held, or the run was
cancelled), or when a renewal fails so the lease cannot be held; past the
procedure's timeout, ansible-runner stops it.
"""

import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import tracing
from .bridge import BridgeError
from .claim import CAPABILITIES, Claimed, Installed, Reach, Refused, Run, bind, claimed
from .events import Event, Stopped, bounded, from_ansible, learned, stated, strike
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
    def claim(
        self, controller_id: str, *, capabilities: Sequence[str], lease_seconds: int, attempt: str
    ) -> dict[str, Any]:
        """Claim the next queued run this worker can do, under a lease."""

    def renew(self, controller_id: str, operation: str, *, lease_seconds: int) -> dict[str, Any]:
        """Extend the lease on a claimed run."""

    def report(self, controller_id: str, operation: str, report: Mapping[str, Any]) -> dict[str, Any]:
        """Send what a claimed run did so far, or how it ended."""


class Runner(Protocol):
    """Runs one bound run, passing each Ansible event to ``on_event`` and asking
    ``should_stop`` as it goes, and stopping it once its procedure's timeout
    passes; answers ansible-runner's final status (``successful``, ``failed``,
    ``timeout``, ``canceled``)."""

    def __call__(
        self, run: Run, *, on_event: Callable[[Mapping[str, Any]], None], should_stop: Callable[[], bool]
    ) -> str:
        """Run ``run`` and answer its final status."""


# Reads the source at the tip of its default branch and answers its catalog
# (``catalog.scan``); it may refuse.
Scan = Callable[[], Mapping[str, Any]]


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
        self._renewed = clock()
        self.stopped: tuple[str, str] | None = None
        # The run's span, which ansible-runner's thread asks this on.
        self.span: Any = None

    def should_stop(self) -> bool:
        if self.stopped is not None:
            return True
        now = self._clock()
        if now - self._renewed >= RENEW_EVERY_SECONDS:
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


# The outcomes that say why a failed run failed, so a run with none of them
# failed before any task did.
_FAILED = frozenset({"failed", "unreachable"})


def _ended(
    run: Run, status: str, result: str | None, events: Sequence[Event], stopped: str | None
) -> tuple[bool, str, str]:
    """Whether a run ansible-runner ended as ``status`` succeeded, and why not.

    A run that succeeded says what its playbook stated it found or did, else
    what its record says shows it worked, which its checks have just held. A
    run that failed with no task failed or machine unreached says the error
    Ansible stopped it with (``events.Stopped``).
    """

    if status == "successful":
        return True, "", result or " ".join(f"{each.rstrip('.')}." for each in run.verification)
    if status == "failed":
        if stopped and not any(event.outcome in _FAILED for event in events):
            return False, "Failed", stopped
        return False, "Failed", "A task failed or a machine could not be reached."
    if status == "timeout":
        return False, "TimedOut", f"It ran past its procedure's {run.timeout_seconds} second timeout."
    return False, "Failed", f"ansible-runner ended it as {status}."


def _finish(
    run: Run, status: str, hold: _Hold, events: list[Event], result: str | None, stopped: str | None
) -> dict[str, Any]:
    kept, left_out = bounded(events)
    if hold.stopped is not None:
        reason, message = hold.stopped
        success = False
    else:
        success, reason, message = _ended(run, status, result, events, stopped)
    if left_out:
        message = f"{message} {left_out} task outcomes that were fine or skipped are not listed.".lstrip()
    return _report(run.generation, success=success, message=message, reason=reason, events=kept)


def run_claimed(
    bridge: Bridge,
    controller_id: str,
    run: Run,
    runner: Runner,
    clock: Callable[[], float],
    secrets: Collection[str] = (),
) -> dict[str, Any]:
    """Run ``run`` under its held lease and answer the report it ends with,
    struck of ``secrets``, of its secret parameters and of its key paths."""

    secrets = learned(run.parameters) | frozenset(secrets) | {str(target.identity) for target in run.targets}
    hold = _Hold(bridge, controller_id, run, clock, secrets)
    events: list[Event] = []
    result: list[str] = []
    stopped = Stopped(run.procedure.project_dir, secrets)

    def on_event(event: Mapping[str, Any]) -> None:
        stopped.read(event)
        if (said := stated(event, secrets)) is not None:
            result.append(said)
        found = from_ansible(event, secrets)
        if found is not None:
            events.append(found)
            # The struck event's host, task and outcome; never its message.
            tracing.event(hold.span, found.outcome, **{"ansible.host": found.host, "ansible.task": found.task})

    with tracing.span("ansible-runner", **{"hq.procedure": run.procedure_id, "hq.hosts": len(run.hosts)}) as ran:
        hold.span = ran
        status = runner(run, on_event=on_event, should_stop=hold.should_stop)
        tracing.note(**{"ansible.status": status})
    report = _finish(run, status, hold, events, result[-1] if result else None, stopped.message)
    if hold.stopped is not None:
        tracing.note(**{"hq.stopped": hold.stopped[0]})
    return report


def _struck(node: Any, secrets: Collection[str]) -> Any:
    """``node`` with ``secrets`` struck from every string in it, at any depth."""

    if isinstance(node, str):
        return strike(node, secrets)
    if isinstance(node, Mapping):
        return {key: _struck(value, secrets) for key, value in node.items()}
    if isinstance(node, list):
        return [_struck(value, secrets) for value in node]
    return node


def scan_claimed(claim: Claimed, scan: Scan, secrets: Collection[str] = ()) -> dict[str, Any]:
    """Read the source ``claim`` asks about and answer the report carrying its
    catalog, struck of ``secrets``.

    A scan changes nothing, so a scan a crash cut short is read again rather
    than reported unknown: the journal never records one as started.
    """

    with tracing.span("scan source"):
        try:
            catalog = scan()
        except Refused as refused:
            message = strike(refused.message, secrets)
            return _report(claim.generation, success=False, message=message, reason=refused.reason)
        tracing.note(**{"hq.procedures": len(catalog["procedures"])})
    message = f"Read commit {catalog['revision'][:12]} on {catalog['branch']}: {len(catalog['procedures'])} playbooks."
    report = _report(claim.generation, success=True, message=strike(message, secrets))
    report["catalog"] = _struck(dict(catalog), secrets)
    return report


def work_once(
    bridge: Bridge,
    *,
    controller_id: str,
    installed: Installed,
    reach: Reach,
    journal: Journal,
    runner: Runner,
    scan: Scan,
    clock: Callable[[], float] = time.monotonic,
    secrets: Collection[str] = (),
) -> Outcome | None:
    """Claim one run or scan and see it through to its report; None when nothing waits.

    ``installed`` resolves the procedure a run names and ``reach`` the
    connection to each of its machines; ``scan`` reads the source a scan asks
    about. ``secrets`` are struck from the report wherever they appear, as a
    run's own secrets are.
    """

    with tracing.span("automation pass", **{"hq.capabilities": ",".join(CAPABILITIES)}):
        outcome = _pass(
            bridge,
            controller_id=controller_id,
            installed=installed,
            reach=reach,
            journal=journal,
            runner=runner,
            scan=scan,
            clock=clock,
            secrets=secrets,
        )
        if outcome is not None:
            tracing.note(**{"hq.operation.id": outcome.operation, "hq.ok": outcome.report["success"] is True})
        return outcome


def _pass(
    bridge: Bridge,
    *,
    controller_id: str,
    installed: Installed,
    reach: Reach,
    journal: Journal,
    runner: Runner,
    scan: Scan,
    clock: Callable[[], float],
    secrets: Collection[str],
) -> Outcome | None:
    entry = journal.begin()
    pending = bridge.claim(controller_id, capabilities=CAPABILITIES, lease_seconds=LEASE_SECONDS, attempt=entry.attempt)
    try:
        claim: Claimed | None = claimed(pending)
    except Refused:
        journal.clear()
        raise
    if claim is None:
        journal.clear()
        return None
    if claim.scans:
        report = scan_claimed(claim, scan, secrets)
    elif entry.operation == claim.operation:
        report = entry.report or _report(claim.generation, success=False, message=UNKNOWN, reason="OutcomeUnknown")
    else:
        try:
            run = bind(claim, installed, reach)
        except Refused as refused:
            message = strike(refused.message, secrets)
            report = _report(claim.generation, success=False, message=message, reason=refused.reason)
        else:
            entry = journal.started(entry, claim.operation)
            report = run_claimed(bridge, controller_id, run, runner, clock, secrets)
            journal.finished(entry, report)
    bridge.report(controller_id, claim.operation, report)
    journal.clear()
    return Outcome(claim.operation, report)
