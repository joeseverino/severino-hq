"""Every surface says a connection's state as ``connection_standing`` derives it."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL
from hq.platform.core.testing import OPERATOR, connection

from ..command_center import command_center
from ..connection_context import connections_context
from ..connection_lifecycle import ATTENTION, WORKING, Lifecycle
from ..connections import ConnectionAbility, ConnectionInstance, ConnectionSpec
from ..estate import connection_states
from ..findings import findings
from ..inventory_testing import store
from ..plugin_testing import offering
from ..security import Capability
from ..topology import derive_topology

READ = ConnectionAbility(
    "example.accounts.read", "Read accounts", "Read accounts.", grant="scoped", required_scopes=("accounts:read",)
)


def _family() -> ConnectionSpec:
    """A family of the two states only an extension's own instances reach:
    one never observed, and one whose credential lacks a permission it needs."""

    instances = (
        ConnectionInstance("example-added", "example-added", "example.bank", credential_model="scoped"),
        ConnectionInstance(
            "example-unauthorized",
            "example-unauthorized",
            "example.bank",
            status="good",
            observed_at=timezone.now(),
            scopes_known=True,
            ability_names=(READ.name,),
            credential_model="scoped",
        ),
    )
    return ConnectionSpec(
        "example.banks",
        "Banks",
        "Example banks.",
        Capability.READ,
        lambda: instances,
        (READ,),
        secret_store="Example store",
    )


class OneStandingTests(TestCase):
    def test_every_state_is_said_alike_on_every_surface(self):
        long_ago = timezone.now() - timedelta(days=60)
        # Each state, raised the way the estate raises it; the last two only an
        # extension's family reaches.
        connection("npm", "example-unprobed", probed=False)
        connection("portainer", "example-working")
        connection("adguard", "example-down", reachable=False, detail="Timed out")
        connection("tailscale", "example-old", observed_at=long_ago, reported_at=long_ago)
        connection("cloudflare_api", "example-refused")
        store("cloudflare.pages_project", reachable=False, refusal=CREDENTIAL_REFUSAL, error="Invalid API token")
        connection("caddy", "example-gone", reachable=False, unreported_at=timezone.now())
        states = {
            "example-unprobed": Lifecycle.UNPROBED,
            "example-working": Lifecycle.REACHABLE,
            "example-down": Lifecycle.UNREACHABLE,
            "example-old": Lifecycle.STALE,
            "example-refused": Lifecycle.REVOKED,
            "example-gone": Lifecycle.UNREPORTED,
            "example-added": Lifecycle.CONFIGURED,
            "example-unauthorized": Lifecycle.UNAUTHORIZED,
        }
        self.assertEqual(set(states.values()), set(Lifecycle))

        with offering(_family()):
            answer = connections_context(principal=OPERATOR).as_dict()
            rows = {item["label"]: item for item in answer["items"]}
            found = command_center("example", principal=OPERATOR, include_live_connections=True)["connections"]
            badges = {item.name.rpartition(":")[2]: item.badges for item in found if item.kind == "connection"}
            topology = derive_topology(principal=OPERATOR).nodes
            nodes = {node.label: (node.status, node.status_label) for node in topology if node.kind == "connection"}
            troubled = {item.ref: item.state for item in connection_states()}
            raised = findings(principal=OPERATOR, rule="connection-not-answering")["findings"]

        for label, state in states.items():
            with self.subTest(state):
                row = rows[label]
                self.assertEqual(
                    (row["lifecycle"], row["status"], row["status_label"]), (state, state.tone, state.label)
                )
                self.assertEqual(badges[label], (state.label,))
                self.assertEqual(nodes[label], (state.tone, state.label))
                # The estate keeps the controller's connections that need a person.
                kept = state in (Lifecycle.UNREACHABLE, Lifecycle.REVOKED, Lifecycle.UNREPORTED)
                self.assertEqual(troubled.get(label), state if kept else None)
                self.assertEqual(
                    any(label in finding["title"] for finding in raised),
                    state in (Lifecycle.UNREACHABLE, Lifecycle.REVOKED),
                )
        summary = answer["summary"]
        self.assertEqual(summary["healthy"], sum(state in WORKING for state in states.values()))
        self.assertEqual(summary["attention"], sum(state in ATTENTION for state in states.values()))
