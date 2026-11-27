"""The adopted playbooks and their repository's scans are read under the
reader's read grant: each playbook with its command, its Run page's facts and
the machines a run may name now, and the newest scan with what HQ refuses in
it, what adopting it widens, and whether it waits for a person."""

from hq.platform.core.testing import MCP_READER

from ..record_errors import RecordNotFound
from ..resources import get_resource
from .test_automation_adoption import AdoptionTestCase, catalog, procedure


class ScanReadTests(AdoptionTestCase):
    def test_a_scan_hq_will_not_adopt_says_why(self):
        unsigned = {"verified": False, "reason": "unsigned", "committer": ""}
        scanned = self.proposed(catalog(procedure("check"), verification=unsigned))

        found = get_resource("playbook_scans", scanned, principal=MCP_READER)

        self.assertTrue(found["refused"])
        with self.assertRaises(RecordNotFound):
            get_resource("playbook_scans", scanned + 1, principal=MCP_READER)
