"""HQ counts itself as a service, on the machine it runs on."""

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.core.testing import declare

from ..hq_self import hq_service

SITE = override_settings(
    SEVERINO_SITE_HOST="hq.example.com",
    ALLOWED_HOSTS=["hq.example.com", "testserver"],
)


def own(*addresses):
    return mock.patch(
        "hq.platform.application.hq_self.host_addresses",
        return_value=frozenset({"127.0.0.1", "::1", *addresses}),
    )


def machine_at(name, *addresses):
    declare(name, "machine", {"name": name, "addresses": list(addresses)})


def tailnet_device(name, addresses, endpoints=()):
    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={
            "records": [
                {
                    "name": name,
                    "online": True,
                    "addresses": list(addresses),
                    "endpoints": list(endpoints),
                }
            ],
            "observed_at": timezone.now(),
        },
    )


@SITE
class TailnetMachineTests(TestCase):
    """A machine known only to the tailnet carries only tailnet addresses."""

    def setUp(self):
        tailnet_device(
            "example-laptop",
            ("100.64.0.5", "fd7a:115c:a1e0::5"),
            ("192.0.2.10:41641", "[2001:db8::10]:41641"),
        )

    def test_an_endpoint_two_devices_report_names_neither(self):
        ProviderInventory.objects.filter(kind="tailscale.device").update(
            records=[
                {"name": "example-laptop", "addresses": ["100.64.0.5"], "endpoints": ["192.0.2.10:41641"]},
                {"name": "example-desk", "addresses": ["100.64.0.6"], "endpoints": ["192.0.2.10:41642"]},
            ]
        )

        with own("192.0.2.10"):
            self.assertEqual(hq_service().machine, "")
