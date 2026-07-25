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

from .providers import ProviderError, execute, preflight


class BridgeError(RuntimeError):
    pass


def _registry() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "controller-capabilities.json"
    )
    registry = json.loads(path.read_text())
    if registry.get("schema_version") != 1:
        raise BridgeError("Unsupported controller capability registry version.")
    return registry


def supported_capabilities() -> tuple[tuple[str, str], ...]:
    capabilities = _registry().get("capabilities", {})
    return tuple(
        sorted(
            (kind, action)
            for kind, capability in capabilities.items()
            for action, policy in capability.get("actions", {}).items()
            if policy.get("mode") == "apply"
        )
    )


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


def run_once(controller_id: str, *, apply: bool) -> int:
    connections = preflight()
    if not apply:
        peek_args = ["peek"]
        for kind, action in supported_capabilities():
            peek_args.extend(("--capability", f"{kind}:{action}"))
        pending = _manage(*peek_args)
        operation = pending.get("operation")
        plan = None
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
        default=_registry().get("controller_id") or os.uname().nodename,
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
