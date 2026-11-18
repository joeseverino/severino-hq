"""Which devices can sign for tailnet lock, and the finding when a server can."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource

from ..inventory_testing import store

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
    ManagedResource.objects.create(
        key="example-server-web",
        kind="portainer.container",
        spec={"connection_ref": "example-portainer", "host": "example-server", "name": "web"},
    )
    store(
        "host.tailnet_lock",
        {"record": "lock", "enabled": enabled, "signers": [{"key": key, "votes": 1} for key in signers]},
    )


class SignerTests(TestCase):
    def test_the_machine_page_says_its_device_signs(self):
        tailnet(signers=(PHONE_KEY,))
        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))

        signing = self.client.get(reverse("control_plane:machine", args=["example-phone"]))
        other = self.client.get(reverse("control_plane:machine", args=["example-server"]))

        self.assertContains(signing, "Signing device")
        self.assertNotContains(other, "Signing device")
