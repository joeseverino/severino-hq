"""The controller container: its isolation, the binary it runs, and the environment it reads."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-controller.sh"
LIBRARY = ROOT / "scripts" / "lib" / "secrets.sh"
DOCKERFILE = ROOT / "Dockerfile"


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


# The image is built without its Dockerfile, so these run from a checkout.
@skipUnless(DOCKERFILE.exists(), "the Dockerfile is not in the image")
class ControllerBinaryTests(SimpleTestCase):
    """The container runs the Go controller the image builds, and reaches HQ in process."""

    def test_the_entrypoint_is_the_binary_the_image_builds(self):
        dockerfile = DOCKERFILE.read_text()
        built = re.search(r"^COPY --from=controller /out/hq-controller (\S+)$", dockerfile, re.M)

        self.assertIsNotNone(built)
        self.assertIn(f"--entrypoint {built.group(1)} ", SCRIPT.read_text())

    def test_the_binary_is_static_and_carries_no_build_paths(self):
        dockerfile = DOCKERFILE.read_text()

        self.assertIn("CGO_ENABLED=0", dockerfile)
        self.assertIn("go build -trimpath", dockerfile)
        self.assertIn("./cmd/hq-controller", dockerfile)
        self.assertIn("GOTOOLCHAIN=local", dockerfile)

    def test_the_bridge_runs_the_manage_py_the_image_carries(self):
        script = SCRIPT.read_text()

        self.assertIn("--env HQ_IN_PROCESS=1", script)
        self.assertIn("--env HQ_MANAGE_PY=/app/manage.py", script)
        self.assertRegex(DOCKERFILE.read_text(), r"(?m)^WORKDIR /app$")
        self.assertNotIn("-m controller_runtime", script)


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
