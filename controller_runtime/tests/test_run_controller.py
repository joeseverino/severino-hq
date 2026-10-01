"""The flags the controller container runs with, held so none silently goes."""

from __future__ import annotations

from pathlib import Path

from django.test import SimpleTestCase

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-controller.sh"


class ControllerIsolationTests(SimpleTestCase):
    def test_the_controller_runs_unprivileged_with_a_private_tmp(self):
        script = SCRIPT.read_text()

        for flag in (
            "--user 10001:10001",
            "--cap-drop ALL",
            "--security-opt no-new-privileges:true",
            # A key validated in /tmp lives in memory, and nothing there runs.
            "--tmpfs /tmp:size=64m,noexec,nosuid,nodev",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, script)
