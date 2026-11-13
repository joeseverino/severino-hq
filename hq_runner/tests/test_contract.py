"""The worker's copies of the bridge contract's bounds are the contract's.

The worker ships without HQ, so it cannot read the contract at run time; each
bound it holds a report or a call to is held here to the declaration in
``bridge_base.json``, so a change there that the worker misses fails here.
"""

import json
import unittest
from pathlib import Path

from django.test import tag

from hq_runner import bridge, events, worker

BASE = Path(__file__).resolve().parents[2] / "hq" / "domains" / "control_plane" / "bridge_base.json"


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
