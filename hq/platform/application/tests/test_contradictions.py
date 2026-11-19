"""Where two connections disagree, each a finding with the fix HQ can offer."""

from contextlib import nullcontext

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.core.testing import MANAGER

from ..derivation_testing import uncached
from ..findings import estate_findings
from ..projection import projection_scope
from ..tailnet import unworn_tags
from .test_paths import PUBLIC_RANGE, estate, store


def raised(rule):
    return estate_findings(principal=MANAGER, rule=rule)


@PUBLIC_RANGE
class ContradictionTests(TestCase):
    def setUp(self):
        estate()

    def test_the_rules_share_each_read_with_the_topology_whoever_asks(self):
        """Gathered beside the topology, a rule reads what it already read,
        so a caller that opened no projection pays what one that did pays."""

        def queries(scope):
            with uncached(), CaptureQueriesContext(connection) as ran, scope:
                estate_findings(principal=MANAGER)
            return len(ran.captured_queries)

        self.assertEqual(queries(nullcontext()), queries(projection_scope()))


@PUBLIC_RANGE
class NotContradictionsTests(TestCase):
    """What looks like a disagreement on a real estate and is not one."""

    def setUp(self):
        estate()

    def test_an_edge_that_proxies_on_to_the_internal_machine_is_a_front(self):
        # Public: through the edge, whose route forwards to lab-1. Inside: lab-1 directly.
        store(
            "caddy.route",
            {"connection_ref": "example-edge", "domain": "shop.example.com", "upstream": "100.64.0.10:8000"},
        )
        store(
            "adguard.rewrite",
            {"domain": "app.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
            {"domain": "shop.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
        )

        with projection_scope():
            from ..paths import path_to

            lines = [route.line for route in path_to("shop.example.com").routes]
        self.assertTrue(any("lab-1" in line and "edge-1" in line for line in lines), lines)
        self.assertEqual(raised("split-horizon-disagrees"), ())


class UnwornTagTests(TestCase):
    def test_a_tag_no_device_wears_is_named_and_ports_are_not_part_of_it(self):
        ProviderInventory.objects.create(
            kind="tailscale.device",
            observed_at=timezone.now(),
            records=[{"name": "example-device", "tags": ["tag:web"]}],
        )

        self.assertEqual(unworn_tags({"tag:web:443", "tag:db:5432", "group:admins"}), ("tag:db",))

    def test_nothing_is_said_before_devices_are_read(self):
        self.assertEqual(unworn_tags({"tag:db"}), ())
