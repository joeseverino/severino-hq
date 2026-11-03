"""What a reader learns of a connection family their capabilities do not permit: nothing.

A family's provider runs under a reader its spec permits, or inside HQ's own
derivation of the queue at the operator's authority, whose results each reader
sees only as far as ``readable_by`` allows.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .. import problems, roles
from ..connection_catalog import connection_catalog
from ..connections import ConnectionInstance, ConnectionSpec
from ..dashboard import dashboard_highlights, operating_snapshot
from ..derivations import uncached
from ..domains import domain_dashboard_sections
from ..estate import cards, overview
from ..inventory_testing import store
from ..plugin_testing import offering
from ..projection import projection_scope
from ..resources import list_resource
from ..security import Capability, cli_principal, web_principal

CONNECTIONS = "hq.estate.connections"


def family(status: str = "good"):
    """A family only infrastructure management may read, with one instance in ``status``, and its provider."""

    instance = ConnectionInstance("example-infra", "Example infrastructure", "example", status, status.title())
    provider = mock.Mock(return_value=(instance,))
    spec = ConnectionSpec(
        "example.infrastructure",
        "Infrastructure authority",
        "Requires infrastructure management.",
        Capability.MANAGE_INFRASTRUCTURE,
        provider,
    )
    return spec, provider


class ReaderTestCase(TestCase):
    def setUp(self):
        # A machine, so the estate card is drawn at all.
        store("tailscale.device", {"name": "example-host", "online": True})
        user = get_user_model().objects.create_user("example-viewer")
        roles.assign(user, roles.Role.VIEWER)
        self.user = get_user_model().objects.get(pk=user.pk)
        self.viewer = web_principal(self.user)

    def listed(self, principal) -> int:
        """How many rows the Connections page lists for ``principal``."""

        with projection_scope():
            return sum(len(group.connections) for group in connection_catalog(principal=principal))


class TheReadersOwnReadsTests(ReaderTestCase):
    """Every read made under the reader's authority that says how many connections there are."""

    READS = {
        "estate card": lambda principal: cards(principal=principal),
        "estate overview": lambda principal: overview(principal=principal).kpis,
        "dashboard sections": lambda principal: [
            card for section in domain_dashboard_sections(principal) for card in section["cards"]
        ],
        "dashboard highlights": lambda principal: [
            card for section in dashboard_highlights(principal)["highlights"] for card in section["cards"]
        ],
        "estate resource": lambda principal: list_resource("estate", principal=principal)["items"],
    }

    def counted(self, read, principal) -> list[str]:
        with uncached(), projection_scope():
            found = read(principal)
        return [
            item["value"] if isinstance(item, dict) else item.value
            for item in found
            if (item.get("id") if isinstance(item, dict) else item.label) in (CONNECTIONS, "Connections")
        ]

    def test_a_reader_the_family_does_not_permit_never_runs_its_provider(self):
        spec, provider = family()

        with offering(spec):
            listed = self.listed(self.viewer)
            for name, read in self.READS.items():
                with self.subTest(name):
                    provider.reset_mock()
                    counted = self.counted(read, self.viewer)
                    provider.assert_not_called()
                    self.assertEqual(counted, [str(listed)] if listed else [])

    def test_a_reader_the_family_permits_counts_its_connections(self):
        spec, provider = family()
        operator = cli_principal()

        with offering(spec):
            listed = self.listed(operator)
            for name, read in self.READS.items():
                with self.subTest(name):
                    provider.reset_mock()
                    counted = self.counted(read, operator)
                    provider.assert_called()
                    self.assertEqual(counted, [str(listed)])
        self.assertGreaterEqual(listed, 1)


class TheSharedQueueTests(ReaderTestCase):
    """HQ's own queue, derived once at the operator's authority, as each reader is shown it."""

    def seen(self, principal, status: str):
        """Every figure the shared queue feeds ``principal``, with the family's instance in ``status``."""

        spec, _provider = family(status)
        with offering(spec), uncached():
            snapshot = operating_snapshot(principal=principal)
            with projection_scope():
                counts = problems.problem_counts(principal)
            found = {
                "queue": snapshot["priority"],
                "queue count": snapshot["priority_count"],
                "action items": list_resource("action.items", principal=principal)["items"],
                "problem counts": counts,
                "connections": list_resource("connection.standing", principal=principal),
            }
            if principal is self.viewer:
                page = self.client.get(reverse("action_items")).content.decode()
                found["action items page names it"] = "Example infrastructure" in page
                found["action item count"] = self.client.get(reverse("action_item_count")).content.decode()
        return found

    def test_a_troubled_instance_reaches_no_reader_the_family_does_not_permit(self):
        self.client.force_login(self.user)

        healthy, troubled = self.seen(self.viewer, "good"), self.seen(self.viewer, "serious")

        for name in healthy:
            with self.subTest(name):
                self.assertEqual(troubled[name], healthy[name])

    def test_the_operator_is_told_of_a_troubled_instance(self):
        operator = cli_principal()

        healthy, troubled = self.seen(operator, "good"), self.seen(operator, "serious")

        for name in ("queue", "queue count", "action items", "problem counts"):
            with self.subTest(name):
                self.assertNotEqual(troubled[name], healthy[name])
