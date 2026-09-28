"""A signing key is reachable only through the connection that declares it, and only by openssl."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import SimpleTestCase

from control_plane.provider_adapters.contracts import ProviderError

from controller_runtime import provider_runtime


class SigningTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        root = Path(self.directory.name)
        (root / "github.key").write_bytes(
            self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        environment = {
            "HQ_CONTROLLER_SSH_DIR": str(root),
            "GITHUB_CONNECTION_REF": "github",
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_openssl_signs_and_the_signature_verifies(self):
        signature = provider_runtime.RUNTIME.sign("github", b"header.claims")

        self.key.public_key().verify(
            signature, b"header.claims", padding.PKCS1v15(), hashes.SHA256()
        )

    def test_a_connection_the_controller_was_not_given_has_no_key(self):
        for name in ("elsewhere", "../github", ".hidden", ""):
            with self.subTest(name=name), self.assertRaises(ProviderError):
                provider_runtime.RUNTIME.sign(name, b"data")

    def test_the_key_never_enters_this_process(self):
        calls = []
        real = subprocess.run

        def watch(command, **kwargs):
            calls.append(command)
            return real(command, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=watch), mock.patch(
            "builtins.open", side_effect=AssertionError("the key was opened in process")
        ):
            provider_runtime.RUNTIME.sign("github", b"data")

        self.assertEqual(calls[0][:4], ["openssl", "dgst", "-sha256", "-sign"])


class CompositionTests(SimpleTestCase):
    def test_the_host_and_image_come_from_what_the_controller_was_started_with(self):
        with mock.patch.dict(
            os.environ,
            {
                "SEVERINO_HQ_SOURCE_REPOSITORY": "example/host",
                "HQ_CONTROLLER_IMAGE": "ghcr.io/example/host/composition@sha256:" + "a" * 64,
            },
        ), mock.patch.dict(os.environ, {"SEVERINO_HQ_PLUGIN_LOCK": ""}):
            found = provider_runtime.RUNTIME.composition()

        self.assertEqual(found["repository"], "example/host")
        self.assertTrue(found["image"].startswith("ghcr.io/example/host/"))
        self.assertEqual(found["extensions"], ())
