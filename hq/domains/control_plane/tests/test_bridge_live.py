"""The bridge on a real Unix socket: calls in flight together, and a whole controller pass.

These run the application the way production does: on its own listener, each
call on a thread with a database connection of its own, against a WAL SQLite
file with one writer at a time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.test import TransactionTestCase, override_settings

from hq.domains.control_plane.bridge_application import application
from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.platform.application import cadence
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.resource_operations import OperationCommand, request_reconcile
from hq.platform.application.security import cli_principal
from hq.platform.core.tests.unix_serving import post, served, socket_directory

from .test_control_plane import certificate_spec, declare_targets

CONTROLLER_SOURCE = Path(settings.BASE_DIR) / "controller"
# The binary the image ships, where the suite runs inside the image.
SHIPPED_CONTROLLER = Path("/usr/local/bin/hq-controller")
# One Django start is seconds; a pass over the warm bridge is a fraction of one.
# A pass slower than this is starting something per call.
PASS_BUDGET_SECONDS = 10.0


class LiveBridge(TransactionTestCase):
    def setUp(self):
        manager = socket_directory()
        directory = manager.__enter__()
        self.socket = directory / "bridge.sock"
        self.addCleanup(manager.__exit__, None, None, None)
        self.enterContext(override_settings(SEVERINO_CONTROLLER_HEARTBEAT=str(directory / "heartbeat")))
        serving = served(application, self.socket)
        serving.__enter__()
        self.addCleanup(serving.__exit__, None, None, None)

    def call(self, target: str, payload: object = None) -> tuple[int, dict]:
        status, body = post(self.socket, target, json.dumps(payload).encode() if payload is not None else b"")
        return status, json.loads(body)


class ConcurrentCallTests(LiveBridge):
    def queue_one_operation(self) -> str:
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(key="example-wildcard", kind="tls.certificate", spec=certificate_spec()),
            principal=cli_principal(),
        )
        queued = request_reconcile(
            OperationCommand(idempotency_key="live-once"),
            principal=cli_principal(),
            current_key=ManagedResource.objects.get(key="example-wildcard").key,
        )
        return str(queued["operation"]["id"])

    @patch("hq.platform.application.resource_operations.controller_action_policy", return_value=(True, "active"))
    def test_one_operation_claimed_by_many_at_once_is_leased_to_one(self, _policy):
        operation = self.queue_one_operation()
        with ThreadPoolExecutor(max_workers=12) as pool:
            answers = list(pool.map(lambda i: self.call(f"/claim?controller-id=controller-{i}"), range(12)))
        self.assertEqual([status for status, _ in answers], [200] * 12, answers)
        leased = [answer["operation"] for _, answer in answers if answer["operation"] is not None]
        self.assertEqual(len(leased), 1, leased)
        self.assertEqual(str(leased[0]["id"]), operation)
        stored = OperationRequest.objects.get(pk=operation)
        self.assertEqual(stored.state, OperationRequest.State.CLAIMED)
        self.assertEqual(stored.claimed_by, leased[0]["claimed_by"])

    def test_writes_in_flight_together_all_land(self):
        def record(index: int) -> tuple[int, dict]:
            controller = f"controller-{index}"
            kind = index % 4
            if kind == 0:
                return self.call(f"/inventory?controller-id={controller}", {"adguard.rewrite": {"ok": True, "records": []}})
            if kind == 1:
                return self.call(
                    f"/steps?controller-id={controller}",
                    [{"step": "adguard.rewrite:reconcile", "subject": f"subject-{index}", "reason": "refused"}],
                )
            if kind == 2:
                return self.call(f"/glance-plan?controller-id={controller}")
            return self.call(f"/schedule?controller-id={controller}")

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=16) as pool:
            answers = list(pool.map(record, range(48)))
        elapsed = time.monotonic() - started
        refused = [(status, answer) for status, answer in answers if status != 200]
        self.assertEqual(refused, [])
        self.assertTrue(all(answer["ok"] for _, answer in answers))
        # Writers queue behind one another; none waits out the busy timeout.
        self.assertLess(elapsed, settings.DATABASES["default"]["OPTIONS"]["timeout"])
        self.assertTrue(cadence.controller_standing().known)
        status, due = self.call("/sweep-due?controller-id=controller-0")
        self.assertEqual(status, 200)
        self.assertIn("age_seconds", due)

    def test_a_refusal_arrives_as_a_problem_over_the_socket(self):
        status, body = self.call("/claim")
        self.assertEqual((status, body["detail"]), (400, "controller-id is required."))
        status, body = post(self.socket, "/nothing")
        self.assertEqual(status, 404)


def controller_binary() -> Path | None:
    """The controller to run a pass with: the image's, or one built from this checkout."""

    if SHIPPED_CONTROLLER.exists():
        return SHIPPED_CONTROLLER
    if shutil.which("go") is None or not CONTROLLER_SOURCE.is_dir():
        return None
    built = Path(tempfile.gettempdir()) / f"hq-controller-test-{os.getpid()}"
    if not built.exists():
        subprocess.run(
            ["go", "build", "-o", str(built), "./cmd/hq-controller"],
            cwd=CONTROLLER_SOURCE, check=True, capture_output=True,
        )
    return built


class Counted:
    """The bridge application, with each call's action and time kept.

    A call is kept when it arrives: the caller may have its answer and be gone
    before the application returns here.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    async def __call__(self, scope, receive, send):
        started = time.monotonic()
        at = len(self.calls)
        self.calls.append((scope["path"].removeprefix("/"), 0.0))
        try:
            await application(scope, receive, send)
        finally:
            self.calls[at] = (self.calls[at][0], time.monotonic() - started)


@skipUnless(controller_binary(), "no controller binary and no Go toolchain to build one")
class ControllerPassTests(TransactionTestCase):
    """The Go controller, run as the launcher runs it, against the live Django bridge."""

    def setUp(self):
        manager = socket_directory()
        directory = manager.__enter__()
        self.socket = directory / "bridge.sock"
        self.addCleanup(manager.__exit__, None, None, None)
        self.enterContext(override_settings(SEVERINO_CONTROLLER_HEARTBEAT=str(directory / "heartbeat")))
        self.bridge = Counted()
        serving = served(self.bridge, self.socket)
        serving.__enter__()
        self.addCleanup(serving.__exit__, None, None, None)

    def run_pass(self, *arguments: str, socket: Path | None = None) -> tuple[int, list[dict], float]:
        started = time.monotonic()
        result = subprocess.run(
            [str(controller_binary()), *arguments],
            env={
                "PATH": os.environ.get("PATH", ""),
                "SEVERINO_BRIDGE_SOCKET": str(socket or self.socket),
                "HQ_CONTROLLER_ID": "example-controller",
            },
            capture_output=True, text=True, timeout=120, check=False,
        )
        elapsed = time.monotonic() - started
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result.returncode, lines, elapsed

    def test_an_apply_pass_runs_against_the_warm_bridge_within_its_budget(self):
        code, lines, elapsed = self.run_pass("--apply")
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1], {"claimed": False, "mode": "apply", "ok": True})
        self.assertEqual(
            [action for action, _ in self.bridge.calls],
            ["registry", "glance-plan", "claim", "sweep-due", "connections", "inventory", "analytics",
             "schedule", "claim", "steps"],
        )
        # HQ recorded the pass: the controller arrived and a sweep landed.
        self.assertTrue(cadence.controller_standing().known)
        self.assertLess(elapsed, PASS_BUDGET_SECONDS)
        self.assertLess(sum(seconds for _, seconds in self.bridge.calls), PASS_BUDGET_SECONDS / 2)

    def test_a_plan_pass_reads_and_records_nothing(self):
        code, lines, _ = self.run_pass()
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1]["mode"], "plan")
        self.assertEqual([action for action, _ in self.bridge.calls], ["registry", "peek"])
        self.assertFalse(cadence.controller_standing().known)

    def test_without_the_bridge_the_pass_fails_and_says_why(self):
        code, lines, _ = self.run_pass("--apply", socket=self.socket.with_name("absent.sock"))
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["ok"], False)
        self.assertIn("HQ is not serving the bridge", lines[-1]["message"])
        self.assertEqual(self.bridge.calls, [])
