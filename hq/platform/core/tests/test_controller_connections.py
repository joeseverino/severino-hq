"""Root runs the renderer the signed image built, from the manifested tree."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

DOCKERFILE = ROOT / "Dockerfile"
UNIT = ROOT / "deploy/systemd/severino-hq-secrets.service"


# The image is built without its Dockerfile, so these run from a checkout.
@unittest.skipUnless(DOCKERFILE.exists(), "the Dockerfile is not in the image")
class RendererDeliveryTests(unittest.TestCase):
    """Root runs the renderer the signed image built, from the manifested tree."""

    def test_the_unit_runs_the_binary_the_image_ships_in_the_root_tree(self):
        dockerfile = DOCKERFILE.read_text()
        self.assertIn("-o /out/hq-secrets ./cmd/hq-secrets", dockerfile)
        copied = "COPY --from=controller /out/hq-secrets /out/hq-host /app/deploy/bin/"
        # After the checkout is copied, so a checkout cannot supply it, and
        # before the manifest is written, so the manifest covers it.
        self.assertLess(dockerfile.index("COPY . /app"), dockerfile.index(copied))
        self.assertLess(dockerfile.index(copied), dockerfile.index("root-tree-manifest.sh /app"))
        started = [line for line in UNIT.read_text().splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(started, ["ExecStart=/usr/local/lib/severino-hq/deploy/bin/hq-secrets"])

    def test_a_locally_built_binary_cannot_enter_the_repository_or_the_image(self):
        for name in (".gitignore", ".dockerignore"):
            with self.subTest(name=name):
                self.assertIn("deploy/bin/", (ROOT / name).read_text().splitlines())

    def test_the_unit_keeps_its_hardening(self):
        unit = UNIT.read_text()
        for directive in (
            "NoNewPrivileges=yes",
            "PrivateDevices=yes",
            "PrivateTmp=yes",
            "ProtectClock=yes",
            "ProtectControlGroups=yes",
            "ProtectHome=yes",
            "ProtectHostname=yes",
            "ProtectKernelLogs=yes",
            "ProtectKernelModules=yes",
            "ProtectKernelTunables=yes",
            "ProtectSystem=strict",
            "RestrictNamespaces=yes",
            "RestrictRealtime=yes",
            "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "MemoryDenyWriteExecute=yes",
            "UMask=0077",
            "RequiresMountsFor=/run/severino-hq-secrets",
            # Narrower than the shell renderer's.
            "RestrictAddressFamilies=AF_UNIX AF_INET",
            "IPAddressDeny=any",
            "IPAddressAllow=127.0.0.1",
            "SocketBindDeny=any",
            "PrivateIPC=yes",
            "MemorySwapMax=0",
            "MemoryMax=512M",
            "TasksMax=128",
            "LimitCORE=0",
            "CapabilityBoundingSet=CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE",
            "SystemCallFilter=@system-service",
        ):
            with self.subTest(directive=directive):
                self.assertIn(directive, unit.splitlines())
        # No token, vault or endpoint is configured in the shipped unit.
        self.assertNotIn("LoadCredential", unit)
        self.assertNotIn("Environment=", unit)

    def test_the_shell_renderer_is_gone(self):
        for name in ("refresh-secrets.sh", "render-controller-env.sh", "list-secret-items.sh", "render-env.jq"):
            with self.subTest(name=name):
                self.assertFalse((ROOT / "scripts" / name).exists())
