"""A record that is not there is one refusal, whichever read looked for it.

Every read path raises ``RecordNotFound`` from record_errors.py, and the
resource layer answers it without a spec having to list it, so no module
needs, or may declare, a not-found or conflict class of its own
(``refusal-classes-in-one-place`` in tools/lint/ast-grep).
"""

import importlib
from pathlib import Path

from django.test import SimpleTestCase, TestCase, tag

from ..record_errors import RecordNotFound
from ..resources import get_resource
from ..security import cli_principal

# Distinctive enough that finding it in a message can only mean it was echoed.
SENT = "sent-by-the-caller-7f3a"

RULES = Path(__file__).resolve().parents[4] / "tools/lint/ast-grep/rules"
# The not-found and conflict classes the rule lets be by name, each in the
# module that declares it; none of them means an HQ record.
NOT_RECORDS = {
    # A registry outside HQ answered and holds nothing: an upstream reading.
    "LookupNotFound": "hq.domains.control_plane.dns_lookup",
    # A job of that kind is already running: exported to extensions by hq_sdk.jobs.
    "JobConflict": "hq.domains.jobs.runner",
    # An Idempotency-Key reused for another request: the machine API's protocol.
    "IdempotencyConflict": "hq.platform.application.idempotency",
}


@tag("source")
class RefusalClassExemptionTests(SimpleTestCase):
    def test_each_exemption_is_a_class_its_module_declares(self):
        """refusal-classes-in-one-place lets these be by name, and
        refusal-class-exemptions-stay-home keeps each name to these modules;
        one nothing declares any more is a name left open."""

        import yaml  # The image's suite collects this module without PyYAML.

        for rule in ("refusal-classes-in-one-place", "refusal-class-exemptions-stay-home"):
            with self.subTest(rule):
                read = yaml.safe_load((RULES / f"{rule}.yml").read_text(encoding="utf-8"))
                names = read["rule"]["has"].get("not", read["rule"]["has"])["regex"]
                self.assertEqual(names, f"^({'|'.join(NOT_RECORDS)})$")
        homes = yaml.safe_load((RULES / "refusal-class-exemptions-stay-home.yml").read_text(encoding="utf-8"))
        self.assertLessEqual(
            {f"{module.replace('.', '/')}.py" for module in NOT_RECORDS.values()}, set(homes["ignores"])
        )
        for name, module in NOT_RECORDS.items():
            with self.subTest(name):
                declared = getattr(importlib.import_module(module), name)
                self.assertTrue(issubclass(declared, Exception), name)
                self.assertEqual(declared.__module__, module)


class MissingRecordReadTests(TestCase):
    def test_every_host_resource_answers_a_missing_record_as_not_found(self):
        cases = {
            "infrastructure": SENT,
            "services": f"{SENT}.example.com",
            "machines": SENT,
            "containers": f"{SENT}:{SENT}",
            "upgrades": f"{SENT}:{SENT}",
            "domains": f"{SENT}.example",
            "relationships": f"machine:{SENT}",
            "service.paths": f"not a host {SENT}",
            "readings": SENT,
            "credentials": SENT,
            "connections": SENT,
            "contact.submissions": 7_340_219,
        }
        for name, identifier in cases.items():
            with self.subTest(name):
                with self.assertRaises(RecordNotFound) as refused:
                    get_resource(name, identifier, principal=cli_principal(), strict=False)
                self.assertNotIn(SENT, str(refused.exception))
                self.assertNotIn(str(identifier), str(refused.exception))
