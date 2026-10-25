"""A record that is not there is one refusal, whichever read looked for it.

Every read path raises ``RecordNotFound`` from record_errors.py, and the
resource layer answers it without a spec having to list it, so no module
needs, or may declare, a not-found or conflict class of its own.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase, TestCase, tag

from ..record_errors import RecordNotFound
from ..resources import get_resource
from ..security import cli_principal

# Distinctive enough that finding it in a message can only mean it was echoed.
SENT = "sent-by-the-caller-7f3a"

ROOT = Path(__file__).resolve().parents[4]
SUFFIXES = ("NotFound", "NotFoundError", "Conflict", "ConflictError")
# Named for what they are, and none of them means an HQ record.
NOT_RECORDS = {
    # A registry outside HQ answered and holds nothing: an upstream reading.
    "hq/domains/control_plane/dns_lookup.py": {"LookupNotFound"},
    # A job of that kind is already running: exported to extensions by hq_sdk.jobs.
    "hq/domains/jobs/runner.py": {"JobConflict"},
    # An Idempotency-Key reused for another request: the machine API's protocol.
    "hq/platform/application/idempotency.py": {"IdempotencyConflict"},
}


def _declared(path: Path):
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.ClassDef) and node.name.endswith(SUFFIXES):
            yield node


@tag("source", "tree")
class OneRefusalClassTests(SimpleTestCase):
    def test_no_module_declares_its_own_not_found_or_conflict(self):
        """A module raises record_errors' two classes; a class of its own is one an adapter would miss."""

        shared = ROOT / "hq/platform/application/record_errors.py"
        found = 0
        for package in ("hq", "hq_sdk"):
            for path in (ROOT / package).rglob("*.py"):
                if "tests" in path.parts or path == shared:
                    continue
                allowed = NOT_RECORDS.get(path.relative_to(ROOT).as_posix(), set())
                for node in _declared(path):
                    found += 1
                    self.assertIn(
                        node.name,
                        allowed,
                        f"{path.relative_to(ROOT)}:{node.lineno} declares {node.name}; raise RecordNotFound or "
                        "RecordConflict (hq/platform/application/record_errors.py) instead",
                    )
        self.assertEqual(found, sum(map(len, NOT_RECORDS.values())), "an exemption names a class that is gone")


class MissingRecordReadTests(TestCase):
    def test_every_host_resource_answers_a_missing_record_as_not_found(self):
        cases = {
            "infrastructure.resources": SENT,
            "services": f"{SENT}.example.com",
            "machines": SENT,
            "containers": f"{SENT}:{SENT}",
            "upgrades": f"{SENT}:{SENT}",
            "domains": f"{SENT}.example",
            "relationships": f"machine:{SENT}",
            "paths": f"not a host {SENT}",
            "readings": SENT,
            "credentials": SENT,
            "connection.standing": SENT,
            "contact.submissions": 7_340_219,
        }
        for name, identifier in cases.items():
            with self.subTest(name):
                with self.assertRaises(RecordNotFound) as refused:
                    get_resource(name, identifier, principal=cli_principal(), strict=False)
                self.assertNotIn(SENT, str(refused.exception))
                self.assertNotIn(str(identifier), str(refused.exception))
