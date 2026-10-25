"""The controller container: its isolation, the binary it runs, and the environment it reads."""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[4]
LAUNCHER = ROOT / "controller" / "host" / "controller.go"
DOCKERFILE = ROOT / "Dockerfile"


# The image is built without its Dockerfile, so these run from a checkout.
@tag("source")
class ControllerBinaryTests(SimpleTestCase):
    """The container runs the Go controller the image builds, and reaches HQ over its socket."""

    def test_the_entrypoint_is_the_binary_the_image_builds(self):
        dockerfile = DOCKERFILE.read_text()
        built = re.search(r"^COPY --from=controller /out/hq-controller (\S+)$", dockerfile, re.MULTILINE)

        self.assertIsNotNone(built)
        self.assertIn(f'"--entrypoint", "{built.group(1)}"', LAUNCHER.read_text())


@tag("source")
class BridgeSocketDeliveryTests(SimpleTestCase):
    """The bridge socket's directory is private from the image on; how it is mounted is controller/host's."""

    def directory(self) -> str:
        layout = (ROOT / "controller" / "host" / "layout.go").read_text()
        socket = re.search(r'BridgeSocket = "(/[^"]+)"', layout)
        self.assertIsNotNone(socket)
        return socket.group(1).rsplit("/", 1)[0]

    def test_the_image_makes_the_directory_this_accounts_alone(self):
        directory = self.directory()
        dockerfile = DOCKERFILE.read_text()
        self.assertRegex(dockerfile, rf"chown -R severino:severino [^\n]*{re.escape(directory)}\b")
        self.assertIn(f"chmod 0700 {directory}", dockerfile)

    def test_the_web_listener_is_not_the_socket(self):
        command = re.search(r"^CMD \[(.+?)\]$", DOCKERFILE.read_text(), re.MULTILINE | re.DOTALL).group(1)
        self.assertIn('"hq.config.asgi:application"', command)
        self.assertNotIn("--uds", command)
        self.assertNotIn("--fd", command)
        self.assertNotIn("bridge", command)
