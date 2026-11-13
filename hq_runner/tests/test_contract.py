"""The worker's copies of the bridge contract's bounds, and of the connections
document's shape, are the contracts'.

The worker ships without HQ, so it cannot read either contract at run time;
each bound it holds a report or a call to is held here to the declaration in
``bridge_base.json``, and each key of the connections document it reads to
the schema HQ's registry emits (``hq-connections.openapi.json``) and the
version and bound the controller reads it at, so a change there that the
worker misses fails here.
"""

import json
import re
import unittest
from pathlib import Path

from django.test import tag

from hq_runner import bridge, events, worker

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "hq" / "domains" / "control_plane" / "bridge_base.json"
CONNECTIONS = ROOT / "controller" / "api" / "hq-connections.openapi.json"
DOCUMENT_GO = ROOT / "controller" / "connections" / "document.go"


@tag("source")
class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schemas = json.loads(BASE.read_text(encoding="utf-8"))["components"]["schemas"]

    def test_a_reported_event_is_held_to_run_event_s_bounds(self):
        run_event = self.schemas["RunEvent"]["properties"]
        self.assertEqual(events.HOST_LIMIT, run_event["host"]["maxLength"])
        self.assertEqual(events.TASK_LIMIT, run_event["task"]["maxLength"])
        self.assertEqual(events.MESSAGE_LIMIT, run_event["message"]["maxLength"])
        self.assertEqual(events.EVENT_LIMIT, self.schemas["ControllerReport"]["properties"]["events"]["maxItems"])
        outcomes = {"ok", "changed", *(outcome for outcome in events._OUTCOMES.values())}
        self.assertLessEqual(outcomes, set(run_event["outcome"]["enum"]))

    def test_a_call_is_held_to_the_bridge_s_bounds(self):
        self.assertEqual(bridge.MAX_BRIDGE_BODY, self.schemas["BridgeBody"]["maxLength"])
        lease = self.schemas["LeaseSeconds"]
        self.assertLessEqual(lease["minimum"], worker.LEASE_SECONDS)
        self.assertLessEqual(worker.LEASE_SECONDS, lease["maximum"])
        self.assertIn("Degraded", self.schemas["ConditionType"]["enum"])
        self.assertLessEqual({"Held", "Cancelled"}, set(self.schemas["Renewal"]["properties"]["reason"]["enum"]))


@tag("source")
class ConnectionsDocumentTests(unittest.TestCase):
    def test_each_key_the_worker_reads_is_one_the_document_declares(self):
        from hq_runner import connections

        schemas = json.loads(CONNECTIONS.read_text(encoding="utf-8"))["components"]["schemas"]
        self.assertLessEqual(set(connections.DOCUMENT_KEYS), set(schemas["Document"]["properties"]))
        self.assertLessEqual(set(connections.CONNECTION_KEYS), set(schemas["Connection"]["properties"]))
        self.assertLessEqual(set(connections.TRANSPORT_KEYS), set(schemas["SSHTransport"]["properties"]))

    def test_the_document_is_read_at_the_controller_s_version_and_bound(self):
        from hq_runner import connections

        source = DOCUMENT_GO.read_text(encoding="utf-8")
        version = re.search(r"^const SchemaVersion = (\d+)$", source, re.MULTILINE)
        bound = re.search(r"^const MaxBytes = 1 << (\d+)$", source, re.MULTILINE)
        self.assertIsNotNone(version)
        self.assertIsNotNone(bound)
        self.assertEqual(connections.SCHEMA_VERSION, int(version.group(1)))
        self.assertEqual(connections.MAX_BYTES, 1 << int(bound.group(1)))
