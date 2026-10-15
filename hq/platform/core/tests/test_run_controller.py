"""The controller container: its isolation, the binary it runs, and the environment it reads."""

import re
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[4]
LAUNCHER = ROOT / "controller" / "host" / "controller.go"
DOCKERFILE = ROOT / "Dockerfile"


# The image is built without its Dockerfile, so these run from a checkout.
@skipUnless(DOCKERFILE.exists(), "the Dockerfile is not in the image")
class ControllerBinaryTests(SimpleTestCase):
    """The container runs the Go controller the image builds, and reaches HQ over its socket."""

    def test_the_entrypoint_is_the_binary_the_image_builds(self):
        dockerfile = DOCKERFILE.read_text()
        built = re.search(r"^COPY --from=controller /out/hq-controller (\S+)$", dockerfile, re.MULTILINE)

        self.assertIsNotNone(built)
        self.assertIn(f'"--entrypoint", "{built.group(1)}"', LAUNCHER.read_text())

    def test_the_binary_is_static_and_carries_no_build_paths(self):
        dockerfile = DOCKERFILE.read_text()

        self.assertIn("CGO_ENABLED=0", dockerfile)
        self.assertIn("go build -trimpath", dockerfile)
        self.assertIn("./cmd/hq-controller", dockerfile)
        self.assertIn("GOTOOLCHAIN=local", dockerfile)


@skipUnless(DOCKERFILE.exists(), "the Dockerfile is not in the image")
class BridgeSocketDeliveryTests(SimpleTestCase):
    """The bridge socket's directory: one path, private from the image on, shared only as a volume."""

    compose = (ROOT / "docker-compose.yml").read_text()

    def socket(self) -> str:
        named = re.findall(r"^\s+SEVERINO_BRIDGE_SOCKET: (\S+)$", self.compose, re.MULTILINE)
        self.assertEqual(len(named), 1)
        return named[0]

    def test_the_socket_is_in_a_named_volume_of_its_own(self):
        directory = self.socket().rsplit("/", 1)[0]
        mounted = re.findall(rf"^\s+- (\w+):{re.escape(directory)}$", self.compose, re.MULTILINE)
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
        command = re.search(r"^CMD \[(.+?)\]$", DOCKERFILE.read_text(), re.MULTILINE | re.DOTALL).group(1)
        self.assertIn('"hq.config.asgi:application"', command)
        self.assertNotIn("--uds", command)
        self.assertNotIn("--fd", command)
        self.assertNotIn("bridge", command)


class SecretFileBindTests(SimpleTestCase):
    """A bind whose source is a file never has docker create it.

    The restart policy starts the web container at boot, before hq-secrets has
    rendered the environment onto the emptied tmpfs. A bind left to create its
    source makes a directory at the file's name, which the renderer refuses to
    write through, and HQ stays down until it is removed by hand.
    """

    def test_every_secret_file_bind_refuses_a_missing_source(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        # Each long-syntax mount: its `- type:` line and the deeper lines under it.
        mounts = re.findall(r"(?m)^( +)- type: bind\n((?:\1  .*\n)+)", compose)
        files = {
            re.search(r"target: (\S+)", body).group(1): body for _, body in mounts if "target: /run/secrets/" in body
        }

        self.assertEqual(sorted(files), ["/run/secrets/severino_controller_ca.pem", "/run/secrets/severino_hq_env"])
        for target, body in files.items():
            with self.subTest(target=target):
                self.assertRegex(body, r"bind:\n +create_host_path: false\n")
