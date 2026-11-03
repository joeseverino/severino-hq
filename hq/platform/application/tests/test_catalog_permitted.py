"""Every catalog says whether its asker is permitted an entry by the rule the use refuses by.

That rule reads ``HELD_ONLY_AT``, so a person or an agent granted a capability
held only by HQ itself is never told an entry needing it is permitted, and
the use it would try is refused.
"""

from django.test import TestCase

from hq_sdk.capabilities import StrictCommand

from ..capabilities import CapabilitySpec, describe_capabilities, execute_capability
from ..connections import ConnectionSpec, describe_connections
from ..integrations import compile_integration_graph, override_integration_graph
from ..resources import EmptyQuery, ResourceSpec, describe_resources, list_resource
from ..security import (
    DEVICE_INTERFACE,
    INTERNAL_INTERFACE,
    AuthorizationError,
    Capability,
    Principal,
    operator_capabilities,
)

# Every door a person or an agent asks through.
CALLERS = ("api", "mcp", "web", DEVICE_INTERFACE)


class _Nothing(StrictCommand):
    pass


def _resource(name: str, capability: str) -> ResourceSpec:
    return ResourceSpec(
        name,
        "Example records",
        "Example records.",
        capability,
        list_handler=lambda: {"items": [], "count": 0},
        list_query_type=EmptyQuery,
    )


def _connection(name: str, capability: str) -> ConnectionSpec:
    return ConnectionSpec(name, "Example gateway", "Example gateway.", capability, lambda: ())


GRAPH = compile_integration_graph(
    capabilities=(
        CapabilitySpec(
            "example.records.read",
            "Read example records.",
            "read",
            Capability.READ,
            _Nothing,
            lambda command, *, principal: {"ok": True},
        ),
        CapabilitySpec(
            "example.records.change",
            "Change example records.",
            "remote_write",
            Capability.MANAGE_INFRASTRUCTURE,
            _Nothing,
            lambda command, *, principal: {"ok": True},
        ),
    ),
    resources=(_resource("example.read", Capability.READ), _resource("example.ending", Capability.END_SESSIONS)),
    connections=(
        _connection("example.read", Capability.READ),
        _connection("example.ending", Capability.END_SESSIONS),
    ),
)


def _as(interface: str, capabilities=None) -> Principal:
    # Every grant there is unless named, so only the interface can keep one out.
    return Principal(
        f"example-{interface}", interface, operator_capabilities() if capabilities is None else capabilities
    )


def _permitted(entries: list[dict]) -> dict[str, bool]:
    return {entry["name"]: entry["permitted"] for entry in entries if entry["name"].startswith("example.")}


class CatalogPermittedTests(TestCase):
    def setUp(self):
        self.enterContext(override_integration_graph(GRAPH))

    def test_an_entry_held_only_by_hq_itself_is_permitted_to_no_person_or_agent(self):
        for interface in CALLERS:
            with self.subTest(interface):
                principal = _as(interface)
                expected = {"example.read": True, "example.ending": False}

                self.assertEqual(_permitted(describe_resources(principal=principal)["resources"]), expected)
                self.assertEqual(_permitted(describe_connections(principal=principal)["connections"]), expected)
                with self.assertRaises(AuthorizationError):
                    list_resource("example.ending", None, principal=principal)

    def test_hq_itself_is_permitted_what_it_alone_holds(self):
        principal = _as(INTERNAL_INTERFACE)

        self.assertTrue(_permitted(describe_resources(principal=principal)["resources"])["example.ending"])
        self.assertTrue(_permitted(describe_connections(principal=principal)["connections"])["example.ending"])

    def test_a_capability_is_permitted_exactly_when_it_runs(self):
        reader = _as("api", frozenset({Capability.READ}))
        permitted = _permitted(describe_capabilities(principal=reader)["capabilities"])

        self.assertEqual(permitted, {"example.records.read": True, "example.records.change": False})
        for name, allowed in permitted.items():
            with self.subTest(name):
                ran = execute_capability(name, {}, principal=reader)
                self.assertEqual(ran.get("ok") is True, allowed, ran)

    def test_without_an_asker_a_catalog_only_describes(self):
        for entries in (describe_resources()["resources"], describe_connections()["connections"]):
            self.assertFalse([entry for entry in entries if "permitted" in entry])
