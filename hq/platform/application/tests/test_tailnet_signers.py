"""Which devices can sign for tailnet lock, and the finding when a server can."""

from django.test import TestCase

from hq.platform.core.testing import READER, declare

from ..inventory_testing import store
from ..resources import get_resource

PHONE_KEY = "tlpub:" + "a1" * 32
SERVER_KEY = "tlpub:" + "b2" * 32


def tailnet(*, signers=(), enabled=True):
    """A phone and a server on the tailnet, each holding its lock key, and
    the lock trusting ``signers``. The server runs a declared container."""

    store(
        "tailscale.device",
        {"name": "example-phone", "os": "iOS", "addresses": ["100.64.0.2"], "lock_key": PHONE_KEY, "online": True},
        {"name": "example-server", "os": "linux", "addresses": ["100.64.0.3"], "lock_key": SERVER_KEY, "online": True},
    )
    declare(
        "example-server-web",
        "portainer.container",
        {"connection_ref": "example-portainer", "host": "example-server", "name": "web"},
    )
    store(
        "host.tailnet_lock",
        {"record": "lock", "enabled": enabled, "signers": [{"key": key, "votes": 1} for key in signers]},
    )


class SignerTests(TestCase):
    def test_the_machine_says_its_device_signs(self):
        tailnet(signers=(PHONE_KEY,))

        for name, signs in (("example-phone", True), ("example-server", False)):
            with self.subTest(name):
                presence = get_resource("machines", name, principal=READER)["presence"]
                self.assertEqual(presence["signs_tailnet_lock"], signs)
