"""The adopted playbooks and their repository's scans are read under the
reader's read grant: each playbook with its command, its Run page's facts and
the machines a run may name now, and the newest scan with the id Adopt takes,
what HQ refuses in it and what adopting it widens."""

from django.test import TestCase

from ..automation_adoption import ADOPT
from ..record_errors import RecordNotFound
from ..resources import get_resource, list_resource
from ..security import AuthorizationError, Capability, Principal, mcp_principal
from .procedures import APP, DB, RESTART, RESTART_RUN, Supply, manifest
from .test_automation_adoption import FIRST, SECOND, AdoptionTestCase, catalog, procedure

READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))
UNREAD = Principal("example-calendar", "mcp", frozenset({Capability.READ_CALENDAR}))
LOGS = "example.service_logs"


class PlaybookReadTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS, effect="read", retry="safe"))

    def test_each_playbook_names_its_command_and_the_machines_a_run_may_name_now(self):
        # The worker reaches the app server as the read login, and the database as another profile.
        self.supply.reach((APP, "read"), (DB, "example.root"))

        listed = {row["playbook"]: row for row in list_resource("playbooks", principal=READER)["items"]}

        self.assertEqual(listed[RESTART]["capability"], RESTART_RUN)
        self.assertEqual(listed[LOGS]["effect"], "read")
        self.assertEqual(listed[RESTART]["reachable"], [APP])

    def test_a_playbook_read_by_its_id_states_what_its_run_page_does(self):
        from ..automation_runs import about

        record = get_resource("playbooks", RESTART, principal=READER)

        shown = {fact.label for fact in about(manifest(), READER).facts}
        self.assertEqual({fact["label"] for fact in record["facts"]}, shown)
        self.assertTrue(all(fact["value"] for fact in record["facts"]), record["facts"])

    def test_an_id_no_playbook_has_is_not_found_and_a_reader_without_the_grant_is_refused(self):
        with self.assertRaises(RecordNotFound):
            get_resource("playbooks", "example.missing", principal=READER)
        with self.assertRaises(AuthorizationError):
            list_resource("playbooks", principal=UNREAD)


class ScanReadTests(AdoptionTestCase):
    def test_the_newest_scan_names_the_id_adopt_takes_and_what_adopting_it_widens(self):
        self.adopt(self.proposed(catalog(procedure("check"))))
        self.proposed(catalog(procedure("check"), procedure("wipe", effect="destructive"), revision=SECOND))

        (newest,) = list_resource("playbook_scans", principal=READER)["items"]

        self.assertEqual((newest["revision"], newest["adopted"], newest["refused"]), (SECOND, False, []))
        self.assertTrue(newest["widens"])
        held = self.adopt(newest["id"], mcp_principal(), key="example-agent-adopt")
        self.assertEqual(held.get("status"), "awaiting_approval", held)
        self.assertEqual(get_resource("playbook_scans", newest["id"], principal=READER)["held"], held["approval"]["id"])
        self.assertEqual(self.adopted(), [FIRST])

    def test_a_scan_hq_will_not_adopt_says_why(self):
        unsigned = {"verified": False, "reason": "unsigned", "committer": ""}
        scanned = self.proposed(catalog(procedure("check"), verification=unsigned))

        found = get_resource("playbook_scans", scanned, principal=READER)

        self.assertTrue(found["refused"])
        with self.assertRaises(RecordNotFound):
            get_resource("playbook_scans", scanned + 1, principal=READER)

    def test_the_scan_read_names_the_command_that_adopts(self):
        from ..resources import describe_resources

        (described,) = describe_resources(name="playbook_scans")["resources"]

        self.assertIn(f"`{ADOPT}`", described["summary"])
