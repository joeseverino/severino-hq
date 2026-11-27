"""Refresh: an operator asks for one connection, one kind or everything to be read.

The forced kinds are due on the next pull whatever the cadence says, and only
the kinds that connection's credential reads. The request goes through the one
gated capability, POST only.
"""

from django.http import QueryDict
from django.test import SimpleTestCase, TestCase, override_settings

from hq.domains.control_plane.models import ReadRequest
from hq.platform.core.testing import connection

from ..action_links import read_now_payload
from ..capabilities import execute_capability
from ..security import cli_principal

READ_NOW = "infrastructure.controller.refresh"
SWEEPS = override_settings(
    SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=3600,
    SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=43200,
)


@SWEEPS
class RefusalTests(TestCase):
    def setUp(self):
        connection("tailscale", "example-tailnet")

    def test_two_subjects_at_once_are_refused(self):
        result = execute_capability(
            READ_NOW,
            {"connection_refs": ["example-tailnet"], "every_connection": True},
            principal=cli_principal(),
        )

        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(ReadRequest.objects.exists())


class PayloadTests(SimpleTestCase):
    def test_the_payload_is_the_inverse_of_the_link(self):
        asked = QueryDict("connection_ref=+example-tailnet+&kind=&kind=tailscale.device&kind=tailscale.device")

        self.assertEqual(
            read_now_payload(asked, QueryDict("connection_ref=example-dns")),
            {"connection_refs": ["example-tailnet", "example-dns"], "kinds": ["tailscale.device"]},
        )
        self.assertEqual(read_now_payload(QueryDict("every_connection=1")), {"every_connection": True})
        self.assertEqual(read_now_payload(QueryDict("every_connection=yes")), {})
