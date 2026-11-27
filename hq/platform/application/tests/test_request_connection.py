"""What HQ can prove about the request in front of it.

The value of this panel is entirely in whether a check can come back false. A
page that says "secure" no matter what is decoration, and decoration in a
security surface is worse than nothing: it is read as a measurement. So most of
what follows takes a fact away and asserts the page notices."""

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory

from ..request_connection import connection
from .connection_requests import A_TAILNET_ADDRESS, a_device, a_request, a_tailnet


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class LayerTests(TestCase):
    def setUp(self):
        self.own_addresses = mock.patch(
            "hq.platform.application.hq_self.host_addresses",
            return_value=frozenset({"100.64.0.9"}),
        )
        self.own_addresses.start()
        self.addCleanup(self.own_addresses.stop)
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test"),
            a_device(
                "hq-host",
                "100.64.0.9",
                observer=True,
                reach=[(443, ["someone@example.test"])],
            ),
        )

    def layer(self, found, layer_id):
        return next(layer for layer in found.checks.results if layer.check.id == layer_id)

    def test_a_published_name_is_not_treated_as_a_defense(self):
        """The request's facts carry what public DNS answers for its name."""

        ProviderInventory.objects.update_or_create(
            kind="cloudflare.dns_record",
            defaults={
                "records": [{"name": "hq.example.test", "content": "203.0.113.7"}],
                "observed_at": timezone.now(),
            },
        )

        found = connection(a_request())

        self.assertFalse(self.layer(found, "name").met)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class IdentityTests(TestCase):
    def test_a_password_session_cannot_use_an_oidc_principal_link(self):
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="operator@passkey"),
            a_device("hq-host", "100.64.0.9", observer=True),
        )
        request = a_request()
        request.user.email = "operator@example.test"
        request.session = {
            "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
            "oidc_tailscale_principal": "operator@passkey",
        }

        self.assertFalse(connection(request).identity.corroborated)


class VerdictTests(SimpleTestCase):
    """Green only when every check on the way in is confirmed."""

    def connection(self, *layers, channel=None, quiet=()):
        """``layers`` as (holds, conclusive) pairs; each index in ``quiet`` a
        check of HQ's standard, left open quietly."""

        from ..request_channel import TAILNET_CHANNEL
        from ..request_connection import Connection
        from ..request_identity import Identity
        from ..request_standard import RequestFacts
        from ..standards import Check, Reading, measure

        def check(index, held, sure):
            reading = Reading.judged(held) if sure else Reading.unavailable("")
            return Check(f"example-{index}", "Example check", lambda _facts: reading, "", "", unmet="")

        standard = tuple(check(index, held, sure) for index, (held, sure) in enumerate(layers))
        facts = RequestFacts(
            request=None, address="100.64.0.20", channel=channel or TAILNET_CHANNEL, identity=Identity()
        )
        return Connection(facts, measure(facts, standard, quiet=tuple(standard[index] for index in quiet)))

    def test_a_check_left_open_quietly_neither_fails_nor_passes(self):
        found = self.connection((False, True), (True, True), quiet=(0,))

        self.assertEqual(found.verdict, "confirmed")
        self.assertEqual(found.summary, "1 of 2 checks passed")
        self.assertEqual(self.connection((False, True), (True, True)).verdict, "failing")
