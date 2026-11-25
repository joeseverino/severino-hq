"""One item per cause, each saying why: what follows from a standing finding is said beneath it."""

from django.test import SimpleTestCase

from hq.platform.core.testing import READER

from ..attention import readable_by
from ..dashboard import finding_rows
from ..finding_fold import CAUSES, beneath_causes, finding_key
from ..finding_model import Finding, matter_key
from ..security import Capability
from ..tone import Tone


def finding(rule: str, severity: Tone = Tone.SERIOUS, **fields) -> Finding:
    return Finding(
        rule=rule,
        subject="machine:example-host",
        title=f"Example {rule}",
        severity=severity,
        explanation="Example reason.",
        **fields,
    )


def key(rule: str) -> str:
    return matter_key(rule, "machine:example-host")


class BeneathCausesTests(SimpleTestCase):
    def test_a_chain_of_causes_is_one_item_at_its_root_severity(self):
        root = finding("example-mount", Tone.ATTENTION)
        failed = finding("example-failed", follows=(key("example-mount"),))
        absent = finding("example-absent", follows=(key("example-failed"),))

        (item,) = beneath_causes((absent, failed, root))

        self.assertEqual(finding_key(item), key("example-mount"))
        self.assertEqual(item.severity, Tone.ATTENTION)
        self.assertEqual(
            {finding_key(each) for each in item.consequences}, {key("example-failed"), key("example-absent")}
        )
        self.assertIn(absent.title, [str(value) for label, value in item.evidence if label == CAUSES])

    def test_a_consequence_stands_alone_once_its_cause_clears(self):
        failed = finding("example-failed", follows=(key("example-mount"),))

        self.assertEqual(beneath_causes((failed,)), (failed,))

    def test_a_loop_of_causes_leaves_its_first_finding_standing(self):
        first = finding("example-first", follows=(key("example-second"),))
        second = finding("example-second", follows=(key("example-first"),))

        (item,) = beneath_causes((first, second))

        self.assertEqual(finding_key(item), key("example-first"))
        self.assertEqual(item.consequences, (second,))


class ReasonTests(SimpleTestCase):
    def test_a_finding_that_says_no_reason_is_refused(self):
        for blank in ("", "  "):
            with self.subTest(explanation=blank), self.assertRaises(ValueError):
                Finding(rule="example", subject="", title="Example", severity=Tone.SERIOUS, explanation=blank)


class ReaderTests(SimpleTestCase):
    """A reader holding only the plain read is told what it may read of a chain."""

    def told(self, cause: tuple[str, ...], consequence: tuple[str, ...]) -> list:
        root = finding("example-mount", read_capabilities=cause)
        failed = finding("example-failed", follows=(key("example-mount"),), read_capabilities=consequence)
        return readable_by(READER, list(finding_rows("hq.findings", "Findings", beneath_causes((failed, root)))))

    def test_what_follows_from_a_cause_it_may_not_read_is_its_item(self):
        (item,) = self.told((Capability.MANAGE_INFRASTRUCTURE,), (Capability.READ,))

        self.assertEqual(item["label"], "Example example-failed")

    def test_a_cause_it_may_read_says_nothing_of_what_it_may_not(self):
        (item,) = self.told((Capability.READ,), (Capability.MANAGE_INFRASTRUCTURE,))

        self.assertEqual(item["label"], "Example example-mount")
        self.assertNotIn("Example example-failed", [fact["value"] for fact in item["facts"]])
        self.assertEqual(item["consequences"], [])
