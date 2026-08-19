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

from .providers import ProviderError, execute, inventory, preflight
from control_plane.providers import (
    controller_capability_registry,
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


def _report_inventory(controller_id: str) -> None:
    """Tell HQ what the providers hold, before doing anything about it.

    Best effort on purpose. This is a convenience -- it powers a page that lists
    what exists and offers to adopt it -- and it must never be the reason an
    operation the operator actually asked for goes unclaimed. A provider that is
    down already reports itself unreachable inside ``inventory``; this catch is
    for the bridge, so a failure to record cannot take out the pass.
    """

    try:
        _manage(
            "inventory",
            "--controller-id",
            controller_id,
            "--payload",
            json.dumps(inventory(), separators=(",", ":")),
        )
    except (BridgeError, ProviderError, OSError, ValueError):
        pass


def run_once(controller_id: str, *, apply: bool) -> int:
    if not apply:
        peek_args = ["peek"]
        for kind, action in supported_capabilities():
            peek_args.extend(("--capability", f"{kind}:{action}"))
        pending = _manage(*peek_args)
        operation = pending.get("operation")
        plan = None
        connections: list[dict[str, Any]] = []
        if operation is not None:
            connections = preflight()
            resource = pending["resource"]
            result = execute(resource, operation["action"], apply=False)
            plan = {
                "operation": operation["id"],
                "resource": resource["key"],
                "action": operation["action"],
                "would_change": result.changed,
                "message": result.message,
            }
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "plan",
                    "claimed": False,
                    "connections": connections,
                    "plan": plan,
                }
            )
        )
        return 0

    _report_inventory(controller_id)
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
        preflight()
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
        default=controller_capability_registry().controller_id or os.uname().nodename,
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
