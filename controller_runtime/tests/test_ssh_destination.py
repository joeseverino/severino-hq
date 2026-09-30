"""An SSH connection's user and host are one login name and one host, never options."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from control_plane.provider_adapters.contracts import ProviderError

from .. import commands
from ..connection_env import ssh_target

BASE = {
    "EDGE_CONNECTION_REF": "example-edge",
    "EDGE_HOST": "edge.example.com",
    "EDGE_PORT": "22",
    "EDGE_USER": "hq-deploy",
    "EDGE_HOST_KEY": "ssh-ed25519 AAAAexample",
    "HQ_CONTROLLER_SSH_DIR": "/run/example-ssh",
}


class DestinationTests(SimpleTestCase):
    def target(self, **changes):
        with mock.patch.dict("os.environ", {**BASE, **changes}, clear=True):
            return ssh_target("example-edge")

    def test_an_ordinary_connection_is_its_user_and_host(self):
        self.assertEqual((self.target()["user"], self.target()["host"]), ("hq-deploy", "edge.example.com"))

    def test_a_user_or_host_that_ssh_would_read_as_an_option_is_refused(self):
        for field, value in (
            ("EDGE_USER", "-oProxyCommand=touch /tmp/example"),
            ("EDGE_USER", "hq@other.example.com"),
            ("EDGE_USER", "hq deploy"),
            ("EDGE_HOST", "-oProxyCommand=true"),
            ("EDGE_HOST", "edge.example.com other"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ProviderError):
                self.target(**{field: value})

    def test_nothing_after_the_options_is_read_as_one(self):
        with mock.patch.dict("os.environ", BASE, clear=True), mock.patch.object(
            commands, "run_command", return_value=b""
        ) as run:
            commands.run_ssh("example-edge", "preflight")

        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--") + 1:], ["hq-deploy@edge.example.com", "preflight"])
        self.assertEqual(Path(argv[argv.index("-i") + 1]).name, "example-edge")
