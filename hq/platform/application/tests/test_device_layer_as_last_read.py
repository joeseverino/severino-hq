"""The device check fails only on a current reading of the tailnet.

A request that arrived over the tailnet came from a device that is on it now.
An old reading that lacks its address has aged, and the check waits for the
next read; a current reading that lacks it is a real failure.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory

from ..request_connection import connection
from .connection_requests import a_device, a_request, a_tailnet

# Longer than a page visit's freshness.
AWAY = timedelta(hours=13)


def read_only_hq(*, age: timedelta = timedelta(0)) -> None:
    """A reading ``age`` old in which HQ's own node is the only device."""

    taken = timezone.now() - age
    a_tailnet(a_device("hq-host", "100.64.0.9", observer=True))
    ProviderInventory.objects.filter(kind="tailscale.device").update(observed_at=taken, updated_at=taken)


def device_layer():
    return next(layer for layer in connection(a_request()).layers if layer.id == "device")


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class DeviceLayerAsLastReadTests(TestCase):
    def test_an_old_reading_without_the_address_waits_for_the_next_read(self):
        read_only_hq(age=AWAY)

        layer = device_layer()

        self.assertFalse(layer.conclusive)
        self.assertIn("last reading", layer.detail)
        self.assertIn("next read", layer.detail)

    def test_a_current_reading_without_the_address_fails(self):
        read_only_hq()

        layer = device_layer()

        self.assertTrue(layer.conclusive)
        self.assertFalse(layer.holds)

    def test_no_reading_cannot_say(self):
        layer = device_layer()

        self.assertFalse(layer.conclusive)
        self.assertIn("has not read", layer.detail)
