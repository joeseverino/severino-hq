"""One pass of the automation worker: claim a run or a scan, hold a run's lease while it runs, and report how it ended.

The runner and the scan are handed in, so a test runs the whole pass with
fakes. The worker stops when HQ answers a renewal with stop or a renewal fails;
past the timeout, ansible-runner stops it.
"""

import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import tracing
from .bridge import BridgeError
from .claim import Claimed, Installed, Reach, Refused, Run, bind, capabilities, claimed
from .events import (
    ELSEWHERE,
    MESSAGE_LIMIT,
    MISSHAPEN,
    PAST_BOUNDS,
    STARTED,
    Event,
    Stated,
    Stopped,
    Tasks,
    bounded,
    from_ansible,
    learned,
    stated,
    strike,
    struck_all,
)
from .journal import Journal

# The lease a claim asks for, and how often a running run renews it: three
# renewals fit in one lease, so one lost renewal never lets it lapse.
LEASE_SECONDS = 120
RENEW_EVERY_SECONDS = 40

# How a run ended (the contract's ``RunOutcome``), which HQ words its result from.
SUCCEEDED = "succeeded"
TASK_FAILED = "task_failed"
UNREACHABLE = "unreachable"
ERRORED = "errored"
TIMED_OUT = "timed_out"
CANCELLED = "cancelled"
PAUSED = "paused"
LEASE_LOST = "lease_lost"
REFUSED = "refused"
UNKNOWN = "unknown"


def ending(kind: str, *, task: str = "", host: str = "", message: str = "") -> dict[str, str]:
    """A ``RunOutcome``: how a run ended, with the task and machine it failed
    on and the ending's own words where it has them."""

    # Cut to the contract's bound as ``events`` cuts a task's message.
    said = message if len(message) <= MESSAGE_LIMIT else message[: MESSAGE_LIMIT - 1] + "…"
    return {"ending": kind, "task": task, "host": host, "message": said}


class Bridge(Protocol):
    def claim(
        self, controller_id: str, *, profile: str, capabilities: Sequence[str], lease_seconds: int, attempt: str
    ) -> dict[str, Any]:
        """Claim the next queued run of ``profile`` this worker can do, under a lease."""

    def renew(
        self, controller_id: str, operation: str, *, lease_seconds: int, progress: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Extend the lease on a claimed run, with its tasks so far (``RunProgress``)."""

    def report(self, controller_id: str, operation: str, report: Mapping[str, Any]) -> dict[str, Any]:
        """Send how a claimed run or scan ended."""


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
    """The run's lease, renewed as it runs with its tasks so far, and why it was told to stop.

    ansible-runner asks ``should_stop`` every few seconds on the thread that
    passes each event, so a task's start or outcome reaches HQ by the next
    ask, carried by a renewal: the one call a worker makes about a run under way.
    """

    def __init__(
        self, bridge: Bridge, controller_id: str, run: Run, clock: Callable[[], float], secrets: frozenset[str]
    ) -> None:
        self._bridge = bridge
        self._controller_id = controller_id
        self._run = run
        self._clock = clock
        self._secrets = secrets
        self._renewed = clock()
        self.tasks = Tasks()
        # How the run ended once it was told to stop (``ending``).
        self.stopped: dict[str, str] | None = None
        # The run's span, which ansible-runner's thread asks this on.
        self.span: Any = None

    def should_stop(self) -> bool:
        if self.stopped is not None:
            return True
        now = self._clock()
        if self.tasks.changed or now - self._renewed >= RENEW_EVERY_SECONDS:
            self._renew(now)
        return self.stopped is not None

    def told(self) -> None:
        """Tell HQ the tasks it has yet to hear once the playbook has ended,
        unless the lease is lost; whether it may go on no longer matters."""

        if self.tasks.changed and (self.stopped is None or self.stopped["ending"] != LEASE_LOST):
            self._renew(self._clock(), ended=True)

    def _renew(self, now: float, *, ended: bool = False) -> None:
        try:
            answer = self._bridge.renew(
                self._controller_id, self._run.operation, lease_seconds=LEASE_SECONDS, progress=self.tasks.progress()
            )
        except BridgeError as exc:
            # A lease that cannot be held lapses, and HQ then treats the run
            # as unknown; stopping now keeps what it did as small as it can be.
            self.stopped = ending(LEASE_LOST, message=strike(str(exc), self._secrets))
            return
        self._renewed = now
        tracing.event(self.span, "lease renewed")
        if not ended and answer.get("continue") is not True:
            self.stopped = ending(CANCELLED if answer.get("reason") == "Cancelled" else PAUSED)


def _scanned(generation: int, *, message: str, reason: str = "") -> dict[str, Any]:
    """A scan's report: whether it read the source (a refusal's ``reason`` where not), in a line."""

    report: dict[str, Any] = {"success": not reason, "observed_generation": generation, "message": message}
    if reason:
        report["conditions"] = [{"type": "Degraded", "status": True, "reason": reason, "message": message}]
    return report


def _ran(
    generation: int, outcome: dict[str, str], *, left_out: str = "", stated: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A run's report: how it ended, what the report leaves out, and what its playbook stated."""

    report: dict[str, Any] = {
        "success": outcome["ending"] == SUCCEEDED,
        "observed_generation": generation,
        "message": left_out,
        "outcome": outcome,
    }
    if stated:
        report["stated"] = dict(stated)
    return report


def _left_out(stated: Stated) -> str:
    """What a run's report says of the parts of what its playbook stated that it leaves out."""

    counts = [(stated.left_out[why], why) for why in (ELSEWHERE, PAST_BOUNDS, MISSHAPEN) if stated.left_out.get(why)]
    if not counts:
        return ""
    total = sum(count for count, _ in counts)
    said = ", ".join(f"{count} {why}" for count, why in counts)
    parts = "1 part of what the playbook stated is" if total == 1 else f"{total} parts of what the playbook stated are"
    return f"{parts} not reported: {said}."


# The task outcomes that say why a failed run failed, so a run with none of
# them failed before any task did.
_FAILED = {"failed": TASK_FAILED, "unreachable": UNREACHABLE}


def _ended(status: str, result: str | None, events: Sequence[Event], stopped: str | None) -> dict[str, str]:
    """How a run ansible-runner ended as ``status`` ended (``ending``).

    One that succeeded carries the result its playbook stated, if any; one
    that failed names its first task to fail or machine unreached, else the
    error Ansible stopped it with (``events.Stopped``).
    """

    if status == "successful":
        return ending(SUCCEEDED, message=result or "")
    if status == "timeout":
        return ending(TIMED_OUT)
    failed = next((event for event in events if event.outcome in _FAILED), None)
    if failed is not None:
        kind = _FAILED[failed.outcome]
        return ending(kind, task=failed.task if kind == TASK_FAILED else "", host=failed.host)
    return ending(ERRORED, message=stopped or f"ansible-runner ended it as {status}.")


def _finish(run: Run, status: str, hold: _Hold, said: Stated | None, stopped: str | None) -> dict[str, Any]:
    hold.told()
    events = hold.tasks.every
    unlisted = bounded(events)[1]
    outcome = hold.stopped or _ended(status, said.result if said else None, events, stopped)
    left_out = " ".join(
        line
        for line in (
            f"{unlisted} task outcomes that were fine or skipped are not listed." if unlisted else "",
            _left_out(said) if said is not None else "",
        )
        if line
    )
    return _ran(run.generation, outcome, left_out=left_out, stated=said.as_report() if said else None)


def run_claimed(
    bridge: Bridge,
    controller_id: str,
    run: Run,
    runner: Runner,
    clock: Callable[[], float],
    secrets: Collection[str] = (),
) -> dict[str, Any]:
    """Run ``run`` under its held lease and answer the report it ends with,
    struck of ``secrets``, of its secret parameters and of its key paths, and
    stating only about ``run``'s own machines (``events.stated``)."""

    secrets = learned(run.parameters) | frozenset(secrets) | {str(target.identity) for target in run.targets}
    hold = _Hold(bridge, controller_id, run, clock, secrets)
    said: list[Stated] = []
    stopped = Stopped(run.procedure.project_dir, secrets)

    def on_event(event: Mapping[str, Any]) -> None:
        stopped.read(event)
        if (found_stated := stated(event, run.hosts, secrets)) is not None:
            said.append(found_stated)
        found = from_ansible(event, secrets)
        if found is not None:
            hold.tasks.add(found)
            # The struck outcome's host, task and outcome; never its message.
            if found.outcome != STARTED:
                tracing.event(hold.span, found.outcome, **{"ansible.host": found.host, "ansible.task": found.task})

    with tracing.span("ansible-runner", **{"hq.procedure": run.procedure_id, "hq.hosts": len(run.hosts)}) as ran:
        hold.span = ran
        status = runner(run, on_event=on_event, should_stop=hold.should_stop)
        tracing.note(**{"ansible.status": status})
    report = _finish(run, status, hold, said[-1] if said else None, stopped.message)
    if hold.stopped is not None:
        tracing.note(**{"hq.stopped": hold.stopped["ending"]})
    return report


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
            return _scanned(claim.generation, message=message, reason=refused.reason)
        tracing.note(**{"hq.procedures": len(catalog["procedures"])})
    message = f"Read commit {catalog['revision'][:12]} on {catalog['branch']}: {len(catalog['procedures'])} playbooks."
    report = _scanned(claim.generation, message=strike(message, secrets))
    report["catalog"] = struck_all(dict(catalog), secrets)
    return report


def work_once(
    bridge: Bridge,
    *,
    controller_id: str,
    profile: str,
    installed: Installed,
    reach: Reach,
    journal: Journal,
    runner: Runner,
    scan: Scan,
    clock: Callable[[], float] = time.monotonic,
    secrets: Collection[str] = (),
) -> Outcome | None:
    """Claim one run or scan and see it through to its report; None when nothing waits.

    Only runs of ``profile``, the one whose logins this worker is lent, are
    claimed. ``installed`` resolves the procedure a run names and ``reach`` the
    connection to each of its machines; ``scan`` reads the source a scan asks
    about. ``secrets`` are struck from the report wherever they appear, as a
    run's own secrets are.
    """

    with tracing.span("automation pass", **{"hq.capabilities": ",".join(capabilities(profile))}):
        outcome = _pass(
            bridge,
            controller_id=controller_id,
            profile=profile,
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
    profile: str,
    installed: Installed,
    reach: Reach,
    journal: Journal,
    runner: Runner,
    scan: Scan,
    clock: Callable[[], float],
    secrets: Collection[str],
) -> Outcome | None:
    entry = journal.begin()
    pending = bridge.claim(
        controller_id,
        profile=profile,
        capabilities=capabilities(profile),
        lease_seconds=LEASE_SECONDS,
        attempt=entry.attempt,
    )
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
        report = entry.report or _ran(claim.generation, ending(UNKNOWN))
    else:
        try:
            run = bind(claim, installed, reach)
        except Refused as refused:
            report = _ran(claim.generation, ending(REFUSED, message=strike(refused.message, secrets)))
        else:
            entry = journal.started(entry, claim.operation)
            report = run_claimed(bridge, controller_id, run, runner, clock, secrets)
            journal.finished(entry, report)
    bridge.report(controller_id, claim.operation, report)
    journal.clear()
    return Outcome(claim.operation, report)
