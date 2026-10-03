"""The isolation flags the controller container runs with, and the environment it reads."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SCRIPT = SCRIPTS / "run-controller.sh"
LIBRARY = SCRIPTS / "lib" / "secrets.sh"


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


class ControllerEnvironmentTests(SimpleTestCase):
    """The controller reads the environment where the refresh rendered it.

    A deploy that binds the tmpfs copy deletes the checkout's. A controller
    that knew only the checkout then failed every run after a deploy that
    reported success, so the choice is one function the reader shares.
    """

    def chosen(self, *, rendered: bool, in_checkout: bool) -> str:
        with tempfile.TemporaryDirectory() as root:
            web, checkout = Path(root, "web"), Path(root, "checkout")
            for directory, present in ((web, rendered), (checkout, in_checkout)):
                directory.mkdir()
                if present:
                    (directory / "severino_hq_env").write_text("KEY=value\n")
            found = subprocess.run(
                ["sh", "-c", '. "$1"; secrets_app_env_dir "$2" "$3"', "sh", LIBRARY, web, checkout],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            return Path(found).name

    def test_the_rendered_copy_is_read_once_there_is_one(self):
        self.assertEqual(self.chosen(rendered=True, in_checkout=True), "web")

    def test_a_deploy_that_removed_the_checkout_copy_leaves_the_controller_its_file(self):
        self.assertEqual(self.chosen(rendered=True, in_checkout=False), "web")

    def test_a_host_no_refresh_has_reached_still_reads_the_checkout(self):
        self.assertEqual(self.chosen(rendered=False, in_checkout=True), "checkout")

    def test_the_controller_asks_rather_than_naming_the_checkout(self):
        script = SCRIPT.read_text()

        self.assertIn('secrets_app_env_dir "${web_secret_dir}" "${app_dir}/secrets"', script)
        self.assertNotIn('app_env="${app_dir}/secrets/severino_hq_env"', script)
        # Whichever directory it is, it is held to the same ownership.
        self.assertIn('secrets_private_dir "${app_env_dir}"', script)
        self.assertIn('secrets_trusted_file "${app_env}" 10001', script)
