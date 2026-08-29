"""One-shot controller: claim, execute, and report exactly one operation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .providers import (
    ProviderError,
    analytics,
    connections,
    execute,
    inventory,
    provider_snapshot,
)
from control_plane.providers import (
    controller_id,
    enabled_controller_actions,
)


class BridgeError(RuntimeError):
    pass


def supported_capabilities() -> tuple[tuple[str, str], ...]:
    return enabled_controller_actions()


def _manage(*args: str) -> dict[str, Any]:
    if os.environ.get("HQ_IN_PROCESS") == "1":
        manage_py = Path(__file__).resolve().parents[1] / "manage.py"
        command = [
            sys.executable,
            str(manage_py),
            "infrastructure_controller",
            *args,
        ]
    else:
        container = os.environ.get("HQ_CONTAINER", "severino-hq")
        docker = os.environ.get("HQ_DOCKER_BIN") or shutil.which("docker")
        if not docker:
            raise BridgeError("Docker CLI was not found.")
        command = [
            docker,
            "exec",
            container,
            "python",
            "manage.py",
            "infrastructure_controller",
            *args,
        ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise BridgeError("HQ controller bridge could not start Docker.") from exc
    if result.returncode:
        raise BridgeError("HQ controller bridge command failed.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError("HQ controller bridge returned invalid JSON.") from exc


def _report(
    controller_id: str,
    operation_id: str,
    *,
    generation: int,
    success: bool,
    status: dict[str, Any],
    conditions: list[dict[str, Any]],
    message: str,
) -> None:
    payload = json.dumps(
        {
            "success": success,
            "observed_generation": generation,
            "status": status,
            "conditions": conditions,
            "message": message,
        },
        separators=(",", ":"),
    )
    _manage(
        "report",
        "--controller-id",
        controller_id,
        "--operation",
        operation_id,
        "--payload",
        payload,
    )


# Kinds whose work needs material HQ is holding rather than credentials the
# controller has. Fetched separately from the contract so it stays out of
# anything that merely describes a resource.
_MATERIAL_KINDS = frozenset({"tls.uploaded_certificate"})


def _with_material(resource: dict[str, Any]) -> dict[str, Any]:
    if resource["kind"] not in _MATERIAL_KINDS:
        return resource
    material = _manage("material", "--resource", resource["key"])
    return {**resource, "spec": {**resource["spec"], "material": material}}


def _post(action: str, controller_id: str, payload: Any) -> None:
    """Tell HQ one thing this controller found, before doing anything about it.

    Best effort on purpose. These are conveniences -- they power the pages that
    list what exists and what HQ can reach -- and neither must ever be the reason
    an operation the operator actually asked for goes unclaimed. A provider that
    is down already reports itself unreachable inside its own sweep; this catch
    is for the bridge, so a failure to record cannot take out the pass.
    """

    try:
        _manage(
            action,
            "--controller-id",
            controller_id,
            "--payload",
            json.dumps(payload, separators=(",", ":")),
        )
    except (BridgeError, ProviderError, OSError, ValueError) as exc:
        # Swallowed, but not silently: stdout is the run's JSON result and is
        # parsed, so this goes to stderr and lands in the journal. A sweep that
        # quietly stopped reporting would leave the pages it feeds looking
        # settled while going stale, which is the failure worth noticing.
        # The type, not the message -- a provider error can name a host or a
        # path, and this line is the one that gets copied into a paste.
        print(f"{action} report skipped: {type(exc).__name__}", file=sys.stderr)


def _report_findings(controller_id: str) -> None:
    """Both sweeps, when HQ says one is due.

    Cadence is HQ's to decide, because HQ is where the observations are: it
    records when each provider was last swept and how stale that may be. This
    asks and executes, the same split as claim, schedule and report -- so the
    interval is a setting rather than a timer, and the timer is free to fire as
    often as applying work needs.

    Connections first: it is the cheaper call and the one that explains the
    other. An inventory that comes back empty because a token expired reads as
    "nothing is out there" on its own, and as one broken credential beside a
    connection sweep that says so.
    """

    try:
        verdict = _manage("sweep-due")
    except BridgeError as exc:
        # Sweeping anyway. HQ being unreachable is a reason to be careful about
        # writing, not about looking, and skipping would make one bad bridge
        # call leave the estate unwatched until the next one succeeds.
        print(f"sweep policy unavailable: {type(exc).__name__}", file=sys.stderr)
    else:
        if not verdict.get("due", True):
            return

    with provider_snapshot():
        try:
            found = connections()
        except (ProviderError, OSError, ValueError) as exc:
            print(f"connections sweep skipped: {type(exc).__name__}", file=sys.stderr)
        else:
            _post("connections", controller_id, found)
        _post("inventory", controller_id, inventory())
        try:
            readings = analytics()
        except (ProviderError, OSError, ValueError) as exc:
            # Its own guard, like connections above. Analytics is the one
            # reading here that leaves the network HQ controls, so it is also
            # the one most able to be slow or refused -- and a page-view count
            # is never a reason for a sweep of the estate to end early.
            print(f"analytics sweep skipped: {type(exc).__name__}", file=sys.stderr)
        else:
            _post("analytics", controller_id, readings)


def run_once(controller_id: str, *, apply: bool) -> int:
    if not apply:
        peek_args = ["peek"]
        for kind, action in supported_capabilities():
            peek_args.extend(("--capability", f"{kind}:{action}"))
        pending = _manage(*peek_args)
        operation = pending.get("operation")
        plan = None
        probed = connections()
        if operation is not None:
            resource = pending["resource"]
            result = execute(resource, operation["action"], apply=False)
            plan = {
                "operation": operation["id"],
                "resource": resource["key"],
                "action": operation["action"],
                "would_change": result.changed,
                "message": result.message,
            }
        healthy = all(connection["ok"] for connection in probed)
        print(
            json.dumps(
                {
                    "ok": healthy,
                    "mode": "plan",
                    "claimed": False,
                    "connections": probed,
                    "plan": plan,
                }
            )
        )
        return 0 if healthy else 1

    _report_findings(controller_id)
    _manage("schedule", "--controller-id", controller_id)
    claim_args = ["claim", "--controller-id", controller_id]
    for kind, action in supported_capabilities():
        claim_args.extend(("--capability", f"{kind}:{action}"))
    claim = _manage(*claim_args)
    operation = claim.get("operation")
    if operation is None:
        print(json.dumps({"ok": True, "mode": "apply", "claimed": False}))
        return 0

    resource = claim["resource"]
    generation = resource["generation"]
    try:
        resource = _with_material(resource)
        result = execute(resource, operation["action"])
    except ProviderError as exc:
        message = str(exc)
        _report(
            controller_id,
            operation["id"],
            generation=generation,
            success=False,
            status=exc.status,
            conditions=[
                {
                    "type": "Degraded",
                    "status": True,
                    "reason": "ProviderError",
                    "message": message,
                }
            ],
            message=message,
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "operation": operation["id"],
                    "resource": resource["key"],
                    "message": message,
                }
            )
        )
        return 1

    _report(
        controller_id,
        operation["id"],
        generation=generation,
        success=True,
        status=result.status,
        conditions=result.conditions,
        message=result.message,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "operation": operation["id"],
                "resource": resource["key"],
                "changed": result.changed,
            }
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--controller-id",
        # One definition, shared with HQ. The registry stopped carrying this
        # when it became an identity rather than a policy, and a second default
        # here would name the same machine differently on each side.
        default=controller_id(),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Claim and execute one operation; omitted means preflight-only plan mode.",
    )
    options = parser.parse_args()
    try:
        return run_once(options.controller_id, apply=options.apply)
    except (BridgeError, ProviderError) as exc:
        print(json.dumps({"ok": False, "message": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
