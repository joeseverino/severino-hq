"""The connection panel says an old reading as last seen, and asks for a fresh one.

Nobody opening HQ for a while is no fault: an idle link and a reading that has
aged since are said as what HQ last saw, never as how things stand now, and
only a disagreement a current reading shows is one.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.core.tests.forwarding import arrived

from ..request_connection import connection
from ..request_path import CONTRADICTED, UNPROVEN, request_path
from .connection_requests import A_TAILNET_ADDRESS, a_device, a_request, a_tailnet
from .markup import select

# How long the panel was left unopened.
AWAY = timedelta(hours=13)


def read(*devices, age: timedelta = timedelta(0)) -> None:
    """The tailnet as a sweep ``age`` ago reported it, each handshake two minutes before that."""

    taken = timezone.now() - age
    a_tailnet(*({**device, "last_handshake": (taken - timedelta(minutes=2)).isoformat()} for device in devices))
    ProviderInventory.objects.filter(kind="tailscale.device").update(observed_at=taken, updated_at=taken)


def laptop(**extra):
    return a_device("a-laptop", A_TAILNET_ADDRESS, **extra)


HQ_HOST = a_device("hq-host", "100.64.0.9", observer=True)


def device_check(request):
    return next(hop.check for hop in request_path(request).hops if hop.step == "device")


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class AsLastReadTests(TestCase):
    def test_an_old_reading_says_the_handshake_as_of_the_read(self):
        read(laptop(direct_endpoint="192.0.2.9:41641"), HQ_HOST, age=AWAY)

        found = connection(a_request())

        self.assertFalse(found.tailnet_current)
        self.assertIn("before the read", found.handshake)
        self.assertNotIn("ago", found.handshake)

    def test_a_current_reading_says_the_handshake_as_an_age(self):
        read(laptop(direct_endpoint="192.0.2.9:41641"), HQ_HOST)

        found = connection(a_request())

        self.assertTrue(found.tailnet_current)
        self.assertTrue(found.handshake.endswith("ago"))

    def test_an_old_reading_names_the_path_as_a_current_one_does(self):
        """The panel's note says when the tailnet was read; the value is the path."""

        read(laptop(direct_endpoint="192.0.2.9:41641"), HQ_HOST, age=AWAY)
        old = connection(a_request()).path_label
        read(laptop(direct_endpoint="192.0.2.9:41641"), HQ_HOST)
        current = connection(a_request()).path_label

        self.assertEqual(old, current)

    def test_an_idle_link_is_connected_and_draws_no_connection_type(self):
        read(laptop(public_key=""), HQ_HOST, age=AWAY)

        found = connection(a_request())

        # This request arrived over it, and the reading names no path.
        self.assertEqual(found.path_label, "Connected")
        self.assertIsNone(found.peering)

    def test_a_device_missing_from_an_old_reading_is_not_a_disagreement(self):
        read(HQ_HOST, age=AWAY)
        old = device_check(a_request())
        read(HQ_HOST)
        current = device_check(a_request())

        self.assertEqual(old.state, UNPROVEN)
        self.assertEqual(current.state, CONTRADICTED)


class PanelTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("panel-reader"))

    def panel(self):
        return self.client.get(reverse("connection"), **arrived(REMOTE_ADDR=A_TAILNET_ADDRESS)).content.decode()

    def test_an_old_reading_is_marked_last_seen_and_the_panel_asks_for_a_fresh_one(self):
        read(laptop(direct_endpoint="192.0.2.9:41641"), HQ_HOST, age=AWAY)

        page = self.panel()

        self.assertTrue(select(page, "data-last-seen"))
        asks = select(page, "data-ask-refresh")
        self.assertEqual(len(asks), 1)
        # Filled in place from its own address, in a dialog as on the page.
        self.assertTrue(select(page, "data-fragment", reverse("connection")))

    def test_a_current_reading_is_said_as_now_and_nothing_is_asked(self):
        read(laptop(direct_endpoint="192.0.2.9:41641"), HQ_HOST)

        page = self.panel()

        self.assertFalse(select(page, "data-last-seen"))
        self.assertFalse(select(page, "data-ask-refresh"))

    def test_an_unread_posture_check_gives_its_cause_in_the_panel(self):
        from ..connection_catalog import connection_catalog
        from ..posture import estate_posture
        from ..security import cli_principal

        unread = [
            standing
            for standing in estate_posture(connection_catalog(principal=cli_principal())).standings
            if standing.reading.met is None
        ]
        page = self.panel()

        shown = {element.attrs["data-posture-unread"]: element.words for element in select(page, "data-posture-unread")}
        self.assertTrue(unread)
        for standing in unread:
            with self.subTest(check=standing.check.id):
                self.assertIn(standing.reading.evidence, shown[standing.check.id])
