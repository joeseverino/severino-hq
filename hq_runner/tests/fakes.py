"""A bridge, a runner and a scan's readers that stand in for HQ, ansible-runner,
ansible-doc and GitHub, so a pass of the worker runs whole with no host,
socket or credential.

The claim answers are shaped as the bridge contract's ``Pending`` (a claimed
run, with its ``input``, or a claimed scan), the runner replays events
ansible-runner 2.4.3 recorded from a real playbook run
(``recorded_events.json``), the role specs are what ansible-doc answered for
the example project's roles (``recorded_role_specs.json``, which the smoke
holds to a real ansible-doc), and a commit's verification is GitHub's answer
for one, in the shape its REST API documents.
"""

import copy
import json
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hq_runner.claim import Procedure, Run

if TYPE_CHECKING:
    from hq_runner.connections import Target

REVISION = "0123456789abcdef0123456789abcdef01234567"
OTHER_REVISION = "89abcdef0123456789abcdef0123456789abcdef"
PROCEDURE = "example.service_restart"
OPERATION = "11111111-2222-4333-8444-555555555555"
APP = "app.example.com"
WORKER = "example-worker"
PLAYBOOK = "playbooks/restart.yml"
PROCEDURES = {PROCEDURE: Procedure(REVISION, Path("/srv/example/project"), PLAYBOOK)}
SSH_DIR = Path("/run/example/ssh")


def connection(ref: str, **transport: str) -> dict[str, Any]:
    """One ssh_transport connection as the root renderer writes it into the
    connections document (``controller/api/hq-connections.openapi.json``)."""

    return {
        "ref": ref,
        "provider": "ssh",
        "manages": False,
        "store": {"vault": "Example", "item": f"example-{ref}"},
        "ssh_transport": {
            "host": "192.0.2.10",
            "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAexample",
            "identity": f"example-{ref}-key",
            "port": "22",
            "user": "example-read",
            **transport,
        },
    }


def write_connections(path: Path, *connections: Mapping[str, Any], schema_version: int = 2, mode: int = 0o600) -> Path:
    """A connections document at ``path``, this account's own file, as the renderer leaves it."""

    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"schema_version": schema_version, "connections": list(connections)}, handle)
    path.chmod(mode)
    return path


def installed(procedure_id: str, revision: str, playbook: str) -> Procedure | None:
    """The worker's procedures as a test has them, by id alone."""

    return PROCEDURES.get(procedure_id)


def reach(host: str) -> Target | None:
    """The one machine a test's worker reaches, through its runner connection."""

    from hq_runner.connections import Target

    if host != APP:
        return None
    return Target(APP, "192.0.2.10", 22, "example-read", SSH_DIR / APP, SSH_DIR / "known_hosts")


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


# The fixture repository in Ansible's sample layout: playbooks, roles, an
# inventory and group_vars at its root, and things a scan reports beside them.
EXAMPLE_PROJECT = Path(__file__).with_name("example_project")
SCAN_OPERATION = "22222222-3333-4444-8555-666666666666"
SOURCE = "automation-source"
# What ``ansible-doc -t role -j <role>`` answered for each of the example
# project's roles, ``path`` aside, which is where the role sat on the machine
# that read it.
RECORDED_ROLE_SPECS: dict[str, Any] = json.loads(
    Path(__file__).with_name("recorded_role_specs.json").read_text("utf-8")
)
# GitHub's answer for one commit (``GET /repos/{owner}/{repo}/commits/{ref}``)
# as its REST API reference documents the response, cut to the members a
# verification is read from and those around them.
GITHUB_COMMIT: dict[str, Any] = {
    "sha": REVISION,
    "node_id": "C_kwDOAAAAAA",
    "commit": {
        "author": {"name": "Example", "email": "automation@example.com", "date": "2026-10-03T12:00:00Z"},
        "committer": {"name": "GitHub", "email": "noreply@github.com", "date": "2026-10-03T12:00:00Z"},
        "message": "procedures",
        "tree": {"sha": OTHER_REVISION, "url": "https://api.github.com/repos/example/automation/git/trees/x"},
        "comment_count": 0,
        "verification": {
            "verified": True,
            "reason": "valid",
            "signature": "-----BEGIN PGP SIGNATURE-----\n...\n-----END PGP SIGNATURE-----",
            "payload": "tree ...",
            "verified_at": "2026-10-03T12:00:01Z",
        },
    },
    "author": {"login": "example-author", "id": 1, "type": "User"},
    "committer": {"login": "web-flow", "id": 19864447, "type": "User"},
    "parents": [],
    "files": [],
}


def role_spec(role: str, playbook_dir: Path, project_dir: Path) -> Any:
    """A role's argument spec as ansible-doc answered it for the example project."""

    from hq_runner.ansible import entry_point

    del playbook_dir, project_dir
    # ansible-doc's answer for a role it cannot find is an empty object.
    return entry_point(role, {role: RECORDED_ROLE_SPECS[role]} if role in RECORDED_ROLE_SPECS else {})


def verified(revision: str) -> dict[str, Any]:
    """The verification GitHub reports of any commit: signed and valid."""

    from hq_runner.catalog import verification

    return verification({**GITHUB_COMMIT, "sha": revision})


def scanned() -> dict[str, Any]:
    """A scan that finds nothing, for a pass that claims none."""

    raise AssertionError("this pass claimed no scan")


def scan_pending(*, generation: int = 1) -> dict[str, Any]:
    """A claimed scan as the bridge's claim answers it: no input, its record's
    generation."""

    return {
        "ok": True,
        "operation": {
            "id": SCAN_OPERATION,
            "resource": SOURCE,
            "action": "scan",
            "state": "claimed",
            "reason": "",
            "requested_actor": "example-person",
            "requested_interface": "web",
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
            "key": SOURCE,
            "kind": "automation.source",
            "generation": generation,
            "enabled": True,
            "spec": {},
            "observed": {},
        },
    }


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
                "playbook": PLAYBOOK,
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

    def claim(
        self, controller_id: str, *, capabilities: Iterable[str], lease_seconds: int, attempt: str
    ) -> dict[str, Any]:
        self.calls.append(
            ("claim", {"controller_id": controller_id, "capabilities": tuple(capabilities), "attempt": attempt})
        )
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
