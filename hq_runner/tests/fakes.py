"""A bridge and a runner that stand in for HQ and ansible-runner, so a pass of
the worker runs whole with no host, socket or credential.

The claim answers are shaped as the bridge contract's ``Pending`` (a claimed
run, with its ``input``), and the runner replays events ansible-runner 2.4.3
recorded from a real playbook run (``recorded_events.json``).
"""

import copy
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from hq_runner.claim import Procedure, Run

REVISION = "0123456789abcdef0123456789abcdef01234567"
OTHER_REVISION = "89abcdef0123456789abcdef0123456789abcdef"
PROCEDURE = "example.service_restart"
OPERATION = "11111111-2222-4333-8444-555555555555"
APP = "app.example.com"
WORKER = "example-worker"
INSTALLED = {PROCEDURE: Procedure(REVISION, Path("/srv/example/project"), "play.yml")}

# Every event of the recorded run, in order: tasks that were fine, changed
# something, were hidden (no_log) and were skipped, then one that failed.
RECORDED: list[dict[str, Any]] = json.loads(Path(__file__).with_name("recorded_events.json").read_text("utf-8"))
# The run up to the task that failed, and its stats: a run that ends well.
WELL = [event for event in RECORDED if event.get("event_data", {}).get("task") != "Fail"]
# Its task outcomes alone, one per task, so a test counts the tasks that ran.
OUTCOMES = [event for event in RECORDED if event["event"] in ("runner_on_ok", "runner_on_failed", "runner_on_skipped")]


def recorded(task: str, event: str) -> dict[str, Any]:
    """One recorded event, by its task's name and kind, to change in a test."""

    found = next(e for e in RECORDED if e["event"] == event and e["event_data"].get("task") == task)
    return copy.deepcopy(found)


def pending(
    *, revision: str = REVISION, parameters: Mapping[str, Any] | None = None, timeout: int = 300
) -> dict[str, Any]:
    """A claimed run as the bridge's claim answers it."""

    return {
        "ok": True,
        "operation": {
            "id": OPERATION,
            "resource": "automation-example-service_restart",
            "action": "run",
            "state": "claimed",
            "reason": "",
            "requested_actor": "example-agent",
            "requested_interface": "mcp",
            "created_at": "2026-10-03T12:00:00+00:00",
            "completed_at": None,
            "claimed_by": WORKER,
            "lease_expires_at": "2026-10-03T12:02:00+00:00",
            "attempt_count": 1,
            "result": None,
            "correlation": "",
        },
        "schema_version": 1,
        "resource": {
            "key": "automation-example-service_restart",
            "kind": "automation.operation",
            "generation": 2,
            "enabled": True,
            "spec": {
                "operation": PROCEDURE,
                "revision": revision,
                "timeout_seconds": timeout,
                "retry": "never",
                "hosts": [],
            },
            "observed": {},
        },
        "input": {
            "generation": 2,
            "operation": PROCEDURE,
            "revision": revision,
            "parameters": dict(parameters or {"service": "web"}),
            "hosts": [APP],
            "correlation": "",
        },
    }


class Bridge:
    """HQ's side of the bridge: answers claims from a queue and renewals as told."""

    def __init__(self, *claims: dict[str, Any], renewals: Iterable[dict[str, Any]] = ()) -> None:
        self.claims = list(claims)
        self.renewals = list(renewals)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reports: list[dict[str, Any]] = []
        self.fail_report = False

    def claim(self, controller_id: str, *, capability: str, lease_seconds: int, attempt: str) -> dict[str, Any]:
        self.calls.append(("claim", {"controller_id": controller_id, "capability": capability, "attempt": attempt}))
        return self.claims.pop(0) if self.claims else {"ok": True, "operation": None}

    def renew(self, controller_id: str, operation: str, *, lease_seconds: int) -> dict[str, Any]:
        self.calls.append(("renew", {"controller_id": controller_id, "operation": operation}))
        if self.renewals:
            return self.renewals.pop(0)
        return {"ok": True, "continue": True, "reason": "", "lease_expires_at": "2026-10-03T12:04:00+00:00"}

    def report(self, controller_id: str, operation: str, report: Mapping[str, Any]) -> dict[str, Any]:
        from hq_runner.bridge import BridgeError

        self.calls.append(("report", {"controller_id": controller_id, "operation": operation}))
        if self.fail_report:
            raise BridgeError("bridge call failed before HQ answered")
        self.reports.append(dict(report))
        return {"ok": True}


class Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Runner:
    """ansible-runner's part: replays ``events``, asking ``should_stop`` after
    each as ansible-runner's loop asks its cancel callback, and ends as it would."""

    def __init__(
        self,
        events: Iterable[Mapping[str, Any]] = (),
        *,
        status: str = "successful",
        between: Callable[[], None] | None = None,
    ) -> None:
        self.events = list(events)
        self.status = status
        self.between = between
        self.runs: list[Run] = []

    def __call__(
        self, run: Run, *, on_event: Callable[[Mapping[str, Any]], None], should_stop: Callable[[], bool]
    ) -> str:
        self.runs.append(run)
        for event in self.events:
            on_event(copy.deepcopy(dict(event)))
            if self.between is not None:
                self.between()
            if should_stop():
                return "canceled"
        return self.status
