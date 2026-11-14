"""The worker's copies of the bridge contract's bounds, and of the connections
document's shape, are the contracts'.

The worker ships without HQ, so it cannot read either contract at run time;
each bound it holds a report or a call to is held here to the declaration in
``bridge_base.json``, and each key of the connections document it reads to
the schema HQ's registry emits (``hq-connections.openapi.json``) and the
version and bound the controller reads it at and the role it lends a runner
connection under, so a change there that the
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
RUNS = ROOT / "hq" / "platform" / "application" / "automation_runs.py"


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

    def test_what_a_run_states_is_held_to_run_stated_s_bounds(self):
        stated = self.schemas["RunStated"]["properties"]
        readings = stated["readings"]["additionalProperties"]["additionalProperties"]
        self.assertEqual(events.RECORD_LIMIT, readings["maxItems"])
        self.assertEqual(events.RECORD_LIMIT, stated["preview"]["additionalProperties"]["maxItems"])
        self.assertEqual(events.TEXT_LIMIT, self.schemas["StatedText"]["maxLength"])
        self.assertEqual(events.HOST_LIMIT, self.schemas["StatedMachine"]["maxLength"])
        self.assertEqual(events.KIND_LIMIT, self.schemas["StatedKind"]["maxLength"])
        self.assertEqual(events.STATED_BYTES, self.schemas["StatedBytes"]["maxLength"])
        self.assertEqual({events.READINGS_STAT, events.PREVIEW_STAT}, set(stated))

    def test_what_the_worker_reports_of_the_recorded_run_conforms_to_run_stated(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        from . import fakes

        contract = json.loads(BASE.read_text(encoding="utf-8"))
        uri = "urn:hq-bridge"
        registry = Registry().with_resource(uri, Resource.from_contents(contract, default_specification=DRAFT202012))
        validator = Draft202012Validator({"$ref": f"{uri}#/components/schemas/RunStated"}, registry=registry)
        said = events.stated(fakes.READINGS, ("app.example.com", "db.example.com")).as_report()

        self.assertEqual(set(said["readings"]), {"app.example.com", "db.example.com"})
        self.assertEqual([error.message for error in validator.iter_errors(said)], [])

    def test_a_scan_s_catalog_is_held_to_source_catalog_s_bounds(self):
        from hq_runner import catalog

        source = self.schemas["SourceCatalog"]["properties"]
        procedure = self.schemas["ScannedProcedure"]["properties"]
        self.assertEqual(catalog.MOST_PROCEDURES, source["procedures"]["maxItems"])
        self.assertEqual(catalog.MOST_FILES, source["files"]["maxItems"])
        self.assertLessEqual(catalog.MOST_FILES, source["unpinned"]["maxItems"])
        self.assertEqual(catalog.MOST_REACHES, source["reaches"]["maxItems"])
        self.assertEqual(catalog.TITLE_LIMIT, procedure["title"]["maxLength"])
        self.assertEqual(catalog.MOST_ROLES, procedure["roles"]["maxItems"])
        self.assertEqual(catalog.MOST_PROBLEMS, procedure["problems"]["maxItems"])
        self.assertEqual(catalog.PROBLEM_LIMIT, procedure["problems"]["items"]["maxLength"])
        for statements in ("preconditions", "verification"):
            self.assertEqual(catalog.MOST_STATEMENTS, procedure[statements]["maxItems"])
            self.assertEqual(catalog.STATEMENT_LIMIT, procedure[statements]["items"]["maxLength"])
        for path in (procedure["sidecar"], self.schemas["ScannedFile"]["properties"]["path"]):
            self.assertEqual(catalog.PATH_LIMIT, path["maxLength"])
        kinds = {found["kind"] for found in catalog.unpinned(["ansible.cfg", "library/x.py", "group_vars/all.yml"])}
        self.assertEqual(kinds, set(self.schemas["UnpinnedPath"]["properties"]["kind"]["enum"]))

    def test_the_recorded_scan_conforms_to_source_catalog(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        contract = json.loads(BASE.read_text(encoding="utf-8"))
        uri = "urn:hq-bridge"
        registry = Registry().with_resource(uri, Resource.from_contents(contract, default_specification=DRAFT202012))
        recorded = json.loads((Path(__file__).with_name("recorded_scan.json")).read_text(encoding="utf-8"))
        validator = Draft202012Validator({"$ref": f"{uri}#/components/schemas/SourceCatalog"}, registry=registry)

        self.assertEqual([error.message for error in validator.iter_errors(recorded)], [])

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

    def test_the_worker_reads_the_role_the_controller_lends(self):
        from hq_runner import connections

        role = re.search(r'^const RunnerRole = "([^"]*)"$', DOCUMENT_GO.read_text(encoding="utf-8"), re.MULTILINE)
        self.assertIsNotNone(role)
        self.assertEqual(connections.ROLE, role.group(1))

    def test_the_read_login_s_profile_is_one_word_on_every_side(self):
        from hq_runner import connections

        profile = re.search(r'^const ReadProfile = "([^"]*)"$', DOCUMENT_GO.read_text(encoding="utf-8"), re.MULTILINE)
        # Read as source, since the worker imports nothing of HQ.
        hq = re.search(r'^READ_PROFILE = "([^"]*)"$', RUNS.read_text(encoding="utf-8"), re.MULTILINE)
        self.assertIsNotNone(profile)
        self.assertIsNotNone(hq)
        self.assertEqual(connections.READ_PROFILE, profile.group(1))
        self.assertEqual(hq.group(1), profile.group(1))
