"""The proxy in front of HQ lets through every upload HQ takes."""

import math

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource
from hq.domains.receipts.validation import MAX_RECEIPT_BYTES
from hq.platform.core.testing import declare, managing_everything

from ..capabilities import execute_capability
from ..projection import projection_scope
from ..security import Capability, Principal, mcp_principal

HQ = "hq.example.com"
EVERYTHING = Principal("test", "operator", frozenset(Capability))


@override_settings(SEVERINO_SITE_HOST=HQ, ALLOWED_HOSTS=[HQ, "testserver"])
class ProxyInFrontOfHQTests(TestCase):
    def test_a_proxy_refusing_an_upload_hq_takes_is_raised_and_its_press_lets_every_upload_through(self):
        from ..findings import estate_findings

        managing_everything()
        spec = {"domain_names": [HQ], "forward_scheme": "http", "forward_host": "192.0.2.10", "forward_port": 8000}
        declare("example-proxy", "npm.proxy_host", {**spec, "force_ssl": False, "client_max_body_size": 1})

        with projection_scope():
            (raised,) = estate_findings(principal=EVERYTHING, rule="proxy-refuses-uploads")
        (press,) = raised.presses
        answer = execute_capability(
            press.capability, {"idempotency_key": "once"}, principal=mcp_principal(), target="example-proxy"
        )
        self.assertTrue(answer["ok"], answer)

        limit = ManagedResource.objects.get(key="example-proxy").spec["client_max_body_size"]
        # A receipt fits, with room for the form around it, and so does one sent as base64 in a call.
        self.assertGreater(limit, math.ceil(MAX_RECEIPT_BYTES / 1024**2))
        self.assertGreaterEqual(limit * 1024**2, 4 * math.ceil(MAX_RECEIPT_BYTES / 3))
        with projection_scope():
            self.assertEqual(estate_findings(principal=EVERYTHING, rule="proxy-refuses-uploads"), ())
