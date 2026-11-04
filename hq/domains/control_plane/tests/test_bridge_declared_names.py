"""The bridge's door strikes no name the contract declares, read from the contract itself,
while a member the contract leaves open is still struck by a secret name."""

import json
from typing import Any

from django.test import TestCase

from hq.domains.control_plane import bridge_application
from hq.domains.control_plane.bridge_actions import ACTIONS, Action
from hq.domains.control_plane.bridge_contract import contract, operations

from . import bridge_client

REDACTED = "[redacted]"


def _properties(node: Any):
    """Every property name a schema node declares, at any depth."""

    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            yield from node["properties"]
        for child in node.values():
            yield from _properties(child)
    elif isinstance(node, list):
        for child in node:
            yield from _properties(child)


def _kinds() -> set[str]:
    schemas = contract()["components"]["schemas"]
    return {kind for name in ("ResourceKind", "SweptKind") for kind in schemas[name]["enum"]}


def _recording(ran: list[tuple[str, Any]]):
    def noted(name: str):
        return lambda parameters, payload: ran.append((name, payload)) or {"ok": True}

    return bridge_application.bridge(tuple(Action(action.name, noted(action.name)) for action in ACTIONS))


def _send(action: str, payload: Any, ran: list[tuple[str, Any]]):
    names = {parameter.name for parameter in operations()[action].parameters}
    query = {name: value for name, value in (("controller-id", "example"), ("operation", "1")) if name in names}
    return bridge_client.request(f"/{action}", query=query, body=json.dumps(payload).encode(), app=_recording(ran))


class DeclaredNameTests(TestCase):
    def test_every_kind_an_inventory_is_keyed_by_passes_unchanged(self):
        kinds = _kinds()
        self.assertTrue(kinds)
        inventory = {kind: {"ok": True, "records": []} for kind in sorted(kinds)}
        self.assertIsNone(operations()["inventory"].violation(inventory))
        ran: list[tuple[str, Any]] = []
        answer = _send("inventory", inventory, ran)
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(ran, [("inventory", inventory)])

    def test_every_property_the_contract_declares_passes_unchanged(self):
        # Each name under a member the contract leaves open, holding a string,
        # the one value a name alone would strike.
        names = set(_properties(contract()["components"]["schemas"]))
        self.assertTrue(names)
        status = dict.fromkeys(sorted(names), "example")
        report = {"success": True, "observed_generation": 1, "status": status, "conditions": [], "message": ""}
        self.assertIsNone(operations()["report"].violation(report))
        ran: list[tuple[str, Any]] = []
        answer = _send("report", report, ran)
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(ran[0][1]["status"], status)

    def test_every_reading_part_a_record_is_keyed_by_passes_unchanged(self):
        # A record's alert counts are keyed by part, and a part's name may
        # sound like a secret (leaked credentials).
        parts = [part for part in contract()["components"]["schemas"]["ReadingPartName"]["enum"] if part]
        self.assertTrue(parts)
        record = {"name": "example", "by_part": dict.fromkeys(parts, "example")}
        inventory = {min(_kinds()): {"ok": True, "records": [record]}}
        ran: list[tuple[str, Any]] = []
        answer = _send("inventory", inventory, ran)
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(ran, [("inventory", inventory)])

    def test_every_query_parameter_the_contract_declares_is_taken(self):
        for operation in operations().values():
            with self.subTest(action=operation.name):
                bridge_application._refuse_secrets(
                    operation, {parameter.name: "example" for parameter in operation.parameters}
                )

    def test_a_credential_in_an_open_record_is_still_struck_by_its_name(self):
        kind = min(_kinds())
        record = {"name": "example", "api_token": "must-never-enter-hq"}
        ran: list[tuple[str, Any]] = []
        with self.assertLogs("severino.bridge", "WARNING") as logged:
            answer = _send("inventory", {kind: {"ok": True, "records": [record]}}, ran)
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(ran[0][1][kind]["records"], [{"name": "example", "api_token": REDACTED}])
        self.assertNotIn("must-never-enter-hq", "\n".join(logged.output))
