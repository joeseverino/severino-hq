"""Root runs the renderer the signed image built, from the manifested tree."""

import unittest
from pathlib import Path

from django.test import tag

ROOT = Path(__file__).resolve().parents[4]

DOCKERFILE = ROOT / "Dockerfile"
UNIT = ROOT / "deploy/systemd/severino-hq-secrets.service"


# The image is built without its Dockerfile, so these run from a checkout.
@tag("source")
class RendererDeliveryTests(unittest.TestCase):
    """Root runs the programs the signed image built, from the tree synced out of it."""

    def test_the_unit_runs_the_binary_the_image_ships_in_the_root_tree(self):
        dockerfile = DOCKERFILE.read_text()
        copied = "COPY --from=controller /out/hq-secrets /out/hq-host /app/deploy/bin/"
        # After the checkout is copied, so a checkout cannot supply them.
        self.assertLess(dockerfile.index(" . /app\n"), dockerfile.index(copied))
        started = [line for line in UNIT.read_text().splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(started, ["ExecStart=/usr/local/lib/severino-hq/deploy/bin/hq-secrets"])

    def test_a_locally_built_binary_cannot_enter_the_repository_or_the_image(self):
        for name in (".gitignore", "Dockerfile.dockerignore"):
            with self.subTest(name=name):
                self.assertIn("deploy/bin/", (ROOT / name).read_text().splitlines())

    def test_the_image_is_built_from_named_paths_only(self):
        lines = [
            line for line in (ROOT / "Dockerfile.dockerignore").read_text().splitlines() if line and line[0] != "#"
        ]
        admitted = [line.removeprefix("!").rstrip("/") for line in lines if line.startswith("!")]

        # Everything is excluded first, so an untracked file cannot slip in.
        self.assertEqual(lines[0], "*")
        self.assertEqual([path for path in admitted if not (ROOT / path).exists()], [])

    def test_the_unit_configures_no_token_vault_or_endpoint(self):
        unit = UNIT.read_text()
        self.assertNotIn("LoadCredential", unit)
        self.assertNotIn("Environment=", unit)
