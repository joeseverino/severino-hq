"""Which devices can sign for tailnet lock, and the finding when a server can."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource

from ..findings import findings
from ..inventory_testing import store
from ..security import Capability, Principal
from ..tailnet_context import tailnet_context

EVERYTHING = Principal("test", "operator", frozenset(Capability))
RULE = "tailnet-lock-server-signs"

PHONE_KEY = "tlpub:" + "a1" * 32
SERVER_KEY = "tlpub:" + "b2" * 32
NOBODYS_KEY = "tlpub:" + "c3" * 32


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


def raised():
    return findings(principal=EVERYTHING, rule=RULE)["findings"]


def signing_devices():
    context = tailnet_context(principal=EVERYTHING)
    return {signer["key"]: signer["machine"] for signer in context.as_dict()["signers"]}


class SignerTests(TestCase):
    def test_a_signing_phone_is_named_and_raises_nothing(self):
        tailnet(signers=(PHONE_KEY,))

        self.assertEqual(signing_devices(), {PHONE_KEY: "example-phone"})
        self.assertEqual(raised(), [])

    def test_a_signing_server_is_serious_and_says_how_to_remove_its_key(self):
        tailnet(signers=(PHONE_KEY, SERVER_KEY))

        (finding,) = raised()

        self.assertEqual((finding["subject"], finding["severity"]), ("machine:example-server", "serious"))
        commands = [step["command"] for step in finding["steps"]]
        self.assertEqual(commands, [f"tailscale lock remove {SERVER_KEY}"])
        self.assertIn("another signing device", finding["steps"][0]["label"])
        # The tailnet page's row of signing devices links to it.
        context = tailnet_context(principal=EVERYTHING)
        (row,) = (setting for setting in context.settings if setting.problem_url)
        self.assertIn(RULE, row.problem_url)
        self.assertIn("example-server", row.value)

    def test_a_key_no_device_holds_is_an_unknown_signer(self):
        tailnet(signers=(PHONE_KEY, NOBODYS_KEY))

        self.assertEqual(signing_devices(), {PHONE_KEY: "example-phone", NOBODYS_KEY: None})
        context = tailnet_context(principal=EVERYTHING)
        (row,) = (setting for setting in context.settings if setting.addresses and setting.label != "Resolves through")
        self.assertEqual(
            [(name.text, bool(name.link)) for name in row.addresses],
            [
                ("example-phone", True),
                ("Unknown signer", False),
            ],
        )
        self.assertEqual(raised(), [])

    def test_with_lock_off_nothing_signs_and_nothing_is_raised(self):
        tailnet(signers=(SERVER_KEY,), enabled=False)

        self.assertEqual(signing_devices(), {})
        self.assertEqual(raised(), [])

    def test_the_machine_page_says_its_device_signs(self):
        tailnet(signers=(PHONE_KEY,))
        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))

        signing = self.client.get(reverse("control_plane:machine", args=["example-phone"]))
        other = self.client.get(reverse("control_plane:machine", args=["example-server"]))

        self.assertContains(signing, "Signing device")
        self.assertNotContains(other, "Signing device")
