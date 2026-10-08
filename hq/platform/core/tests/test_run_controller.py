"""The controller container: its isolation, the binary it runs, and the environment it reads."""

import re
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "run-controller.sh"
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
    """The container runs the Go controller the image builds, and reaches HQ over its socket."""

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

    def test_the_container_reaches_hq_through_the_bridge_socket_and_holds_nothing_of_its_own(self):
        script = SCRIPT.read_text()

        self.assertIn('--env "SEVERINO_BRIDGE_SOCKET=${bridge_socket}"', script)
        # No interpreter is started to reach HQ, and nothing of HQ's is mounted
        # for one to read: not its database, not its application environment.
        for absent in ("HQ_IN_PROCESS", "manage.py", "target=/data", "severino_hq_env", "docker exec"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, script)


@skipUnless(DOCKERFILE.exists(), "the Dockerfile is not in the image")
class BridgeSocketDeliveryTests(SimpleTestCase):
    """The bridge socket's directory: one path, private from the image on, shared only as a volume."""

    compose = (ROOT / "docker-compose.yml").read_text()

    def socket(self) -> str:
        named = re.findall(r"^\s+SEVERINO_BRIDGE_SOCKET: (\S+)$", self.compose, re.M)
        self.assertEqual(len(named), 1)
        return named[0]

    def test_the_socket_is_in_a_named_volume_of_its_own(self):
        directory = self.socket().rsplit("/", 1)[0]
        mounted = re.findall(rf"^\s+- (\w+):{re.escape(directory)}$", self.compose, re.M)
        self.assertEqual(len(mounted), 1)
        self.assertRegex(self.compose, rf"(?m)^volumes:\n(?:  \w+:\n)*  {mounted[0]}:$")
        # Nothing else is mounted at or under it, and it is not a host path.
        self.assertEqual(self.compose.count(directory), 2)

    def test_the_image_makes_the_directory_this_accounts_alone(self):
        directory = self.socket().rsplit("/", 1)[0]
        dockerfile = DOCKERFILE.read_text()
        self.assertRegex(dockerfile, rf"chown -R severino:severino [^\n]*{re.escape(directory)}\b")
        self.assertIn(f"chmod 0700 {directory}", dockerfile)

    def test_the_web_listener_is_not_the_socket(self):
        command = re.search(r'^CMD \[(.+?)\]$', DOCKERFILE.read_text(), re.M | re.S).group(1)
        self.assertIn('"hq.config.asgi:application"', command)
        self.assertNotIn("--uds", command)
        self.assertNotIn("--fd", command)
        self.assertNotIn("bridge", command)
