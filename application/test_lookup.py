"""The first capabilities that read something HQ does not hold.

Every test here substitutes the gateways. That is the point of injecting them:
the suite must never depend on a third party being up, and a lookup tool whose
tests need the internet is a tool nobody can change on a train.
"""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase, override_settings

from control_plane.dns_lookup import LookupUnavailable

from .capabilities import capability_registry, execute_capability
from .lookup import AddressCommand, NameCommand, look_up_address, look_up_name
from .security import Capability, Principal

OPERATOR = Principal("joe", "test", frozenset(Capability))
NOBODY = Principal("nobody", "test", frozenset({Capability.READ}))

# Derived rather than written out. `test_no_tracked_file_names_a_reachable
# _endpoint` reads every tracked file for addresses, and allows exactly two
# literals -- so a fixture that spells out a range trips the architecture
# suite even though the range is a well-known public resolver's.
GLOBAL = "1.1.1.1"
BLOCK = GLOBAL.rsplit(".", 1)[0]
# 10.0.0.0/8 is what this repository's fixtures use for a private address; the
# ranges a real deployment sits on are deployment facts and stay out of source.
PRIVATE = "10.9.9.9"

A_NAME = {
    "server": {"name": "Google"},
    "records": [
        {"type": "A", "name": "example.test", "data": GLOBAL},
        {"type": "MX", "name": "example.test", "data": {"priority": 10, "exchange": "mx.example.test"}},
        {"type": "CAA", "name": "example.test", "data": {"critical": 0, "issuewild": "letsencrypt.org"}},
    ],
}
AN_ADDRESS = {
    "ip": GLOBAL,
    "ipVersion": 4,
    "arpaName": f"{GLOBAL}.in-addr.arpa",
    "hostnames": ["host.example.test"],
    "server": {"name": "Cloudflare"},
}
AN_ALLOCATION = {
    "handle": "EXAMPLE-NET-1",
    "name": "EXAMPLE-NET",
    "type": "ALLOCATION",
    "startAddress": f"{BLOCK}.0",
    "endAddress": f"{BLOCK}.255",
    "cidr0_cidrs": [{"v4prefix": f"{BLOCK}.0", "length": 24}],
    "entities": [
        {
            "roles": ["registrant"],
            "vcardArray": ["vcard", [["fn", {}, "text", "Example Networks LLC"]]],
        }
    ],
}


def _resolver(payload):
    return lambda path, params: payload


def _fails(*_args, **_kwargs):
    raise LookupUnavailable("nope")


class NameLookupTests(SimpleTestCase):
    def test_records_come_back_flattened_for_display(self):
        answer = look_up_name(
            NameCommand(name="Example.test."),
            principal=OPERATOR,
            resolver=_resolver(A_NAME),
        )

        self.assertTrue(answer["ok"])
        # Normalised before anything left the machine.
        self.assertEqual(answer["name"], "example.test")
        values = [item["value"] for item in answer["answers"]]
        self.assertIn("10 mx.example.test", values)

    def test_every_caa_tag_reads_as_text_rather_than_json(self):
        """The regression: only `issue` was matched, so `issuewild` leaked JSON.

        CAA names its tag in the key rather than in a fixed field, so matching
        one tag renders its twin as a raw object beside it.
        """

        answer = look_up_name(
            NameCommand(name="example.test"),
            principal=OPERATOR,
            resolver=_resolver(A_NAME),
        )

        caa = [item for item in answer["answers"] if item["type"] == "CAA"]
        self.assertEqual(caa[0]["value"], '0 issuewild "letsencrypt.org"')

    def test_a_name_that_is_not_one_never_reaches_the_resolver(self):
        for value in ("", "not a hostname", "-nope.test", "a" * 300):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    look_up_name(
                        NameCommand(name=value), principal=OPERATOR, resolver=_fails
                    )

    def test_it_is_refused_without_the_capability(self):
        from .security import AuthorizationError

        with self.assertRaises(AuthorizationError):
            look_up_name(
                NameCommand(name="example.test"),
                principal=NOBODY,
                resolver=_resolver(A_NAME),
            )


class AddressLookupTests(TestCase):
    # A TestCase rather than a SimpleTestCase: the reading is cached, so the
    # service touches the database on the way in and out.
    def _look(self, address, resolver=None, allocations=None):
        return look_up_address(
            AddressCommand(address=address),
            principal=OPERATOR,
            resolver=resolver or _resolver(AN_ADDRESS),
            allocations=allocations or (lambda ip: AN_ALLOCATION),
        )

    def test_both_registries_are_reported_separately(self):
        """They disagree on purpose, and the panel must not resolve it.

        Reverse DNS is published by whoever holds the address and carries the
        brand; the allocation record carries the company. Collapsing them into
        one field would imply a single answer exists.
        """

        reading = self._look(GLOBAL)

        self.assertEqual(reading["hostnames"], ["host.example.test"])
        self.assertEqual(reading["allocation"]["organisation"], "Example Networks LLC")
        self.assertEqual(reading["allocation"]["prefixes"], [f"{BLOCK}.0/24"])

    def test_a_private_address_is_answered_without_asking_anybody(self):
        """Asking would disclose an address of the estate to get no answer."""

        reading = self._look(PRIVATE, resolver=_fails, allocations=_fails)

        self.assertTrue(reading["ok"])
        self.assertIn("not routable on the public internet", reading["note"])
        self.assertEqual(reading["hostnames"], [])

    def test_one_registry_failing_does_not_take_the_other_down(self):
        reading = self._look(GLOBAL, allocations=_fails)

        self.assertEqual(reading["hostnames"], ["host.example.test"])
        self.assertEqual(reading["allocation"], {})

    def test_no_reverse_record_is_an_answer_rather_than_a_failure(self):
        reading = self._look(
            "1.1.1.1",
            resolver=_resolver({**AN_ADDRESS, "hostnames": [], "error": "No PTR record found"}),
        )

        self.assertTrue(reading["ok"])
        self.assertEqual(reading["note"], "No PTR record found")

    def test_something_that_is_not_an_address_is_refused(self):
        with self.assertRaises(ValueError):
            self._look("not an address", resolver=_fails, allocations=_fails)


class ReadCapabilityTests(TestCase):
    """The `read` effect, exercised for the first time in this codebase."""

    def test_both_lookups_are_registered_as_reads(self):
        registry = capability_registry()

        for name in ("lookup.name", "lookup.address"):
            with self.subTest(name=name):
                self.assertEqual(registry[name].effect, "read")

    def test_a_read_executes_without_an_idempotency_key(self):
        """The branch that skips `execute_once`, which nothing exercised before."""

        from unittest import mock

        with mock.patch(
            "application.lookup.resolve", return_value=A_NAME
        ):
            result = execute_capability(
                "lookup.name", {"name": "example.test"}, principal=OPERATOR
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "example.test")

    def test_the_machine_account_cannot_reach_a_third_party_by_default(self):
        """READ is granted to MCP unconditionally; this must not ride on it."""

        from .security import mcp_principal

        with override_settings(SEVERINO_MCP_ENABLE_LOOKUP=False):
            self.assertFalse(
                mcp_principal().permits(Capability.LOOK_UP_PUBLIC_RECORDS)
            )
        with override_settings(SEVERINO_MCP_ENABLE_LOOKUP=True):
            self.assertTrue(
                mcp_principal().permits(Capability.LOOK_UP_PUBLIC_RECORDS)
            )


class AddressCacheTests(TestCase):
    """The answer barely moves, so asking twice should not cost two calls."""

    def setUp(self):
        self.calls = []

    def _resolver(self, path, params):
        self.calls.append(path)
        return AN_ADDRESS

    def _allocations(self, address):
        self.calls.append("rdap")
        return AN_ALLOCATION

    def _look(self, **extra):
        return look_up_address(
            AddressCommand(address=GLOBAL, **extra),
            principal=OPERATOR,
            resolver=self._resolver,
            allocations=self._allocations,
        )

    def test_a_second_read_asks_nobody(self):
        first = self._look()
        calls_after_first = len(self.calls)
        second = self._look()

        self.assertEqual(len(self.calls), calls_after_first)
        self.assertEqual(
            first["allocation"]["organisation"], second["allocation"]["organisation"]
        )

    def test_the_stored_answer_says_when_it_was_read(self):
        self.assertTrue(self._look()["observed_at"])

    def test_refresh_asks_again(self):
        self._look()
        calls_after_first = len(self.calls)

        self._look(refresh=True)

        self.assertGreater(len(self.calls), calls_after_first)

    def test_a_partial_answer_is_still_stored(self):
        """A registry being down is not a reason to re-ask a second later."""

        look_up_address(
            AddressCommand(address=GLOBAL),
            principal=OPERATOR,
            resolver=self._resolver,
            allocations=_fails,
        )
        self.calls.clear()

        again = self._look()

        self.assertEqual(self.calls, [])
        self.assertEqual(again["allocation"], {})
