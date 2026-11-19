"""One answer to which documentation an AI-facing surface may name."""

from django.test import SimpleTestCase, TestCase

from hq.domains.docs_index.models import DocumentationRecord
from hq.platform.core.testing import operator

from ..documentation import serialize_documentation
from ..record_errors import RecordNotFound
from ..resources import get_resource, list_resource
from ..security import MCP_INTERFACE, Capability, Principal, web_principal
from ..sensitivity import SAFE_SENSITIVITIES


class SafeSensitivityTests(SimpleTestCase):
    def test_it_agrees_with_the_record_itself(self):
        """The model's own export check and the query filter are the same set."""

        exportable = {
            value
            for value in DocumentationRecord.Sensitivity.values
            if DocumentationRecord(sensitivity=value).is_safe_for_ai_export
        }

        self.assertEqual(set(SAFE_SENSITIVITIES), exportable)

    def test_every_safe_value_is_a_declared_choice(self):
        """Stated as strings so the model can import it; still only real choices."""

        for value in SAFE_SENSITIVITIES:
            with self.subTest(value=value):
                self.assertIn(value, DocumentationRecord.Sensitivity.values)
        self.assertEqual(
            set(SAFE_SENSITIVITIES),
            {
                DocumentationRecord.Sensitivity.PUBLIC,
                DocumentationRecord.Sensitivity.INTERNAL,
            },
        )

    def test_the_record_reads_the_one_set(self):
        """Not an equal copy: the model's check is this set, so it cannot drift."""

        from hq.domains.docs_index.models import SAFE_SENSITIVITIES as MODEL_SET

        self.assertIs(MODEL_SET, SAFE_SENSITIVITIES)

    def test_sensitive_and_restricted_documentation_is_never_safe(self):
        for level in (
            DocumentationRecord.Sensitivity.SENSITIVE,
            DocumentationRecord.Sensitivity.RESTRICTED,
        ):
            with self.subTest(level=level):
                self.assertNotIn(level, SAFE_SENSITIVITIES)


class SensitiveReadTests(TestCase):
    """A person reads every doc whole; an agent, whatever its grant names, reads what the AI-export rule leaves."""

    @classmethod
    def setUpTestData(cls):
        for level in DocumentationRecord.Sensitivity.values:
            DocumentationRecord.objects.create(
                doc_id=f"example-{level}", title=f"Example {level}", sensitivity=level, obsidian_path=f"x/{level}.md"
            )
        cls.person = web_principal(operator())
        # Granted the capability by name, at a door that never holds it.
        cls.agent = Principal(
            "example-agent", MCP_INTERFACE, frozenset({Capability.READ, "read_sensitive_documentation"})
        )

    def test_an_agent_finds_no_doc_the_rule_keeps_from_it(self):
        for level in DocumentationRecord.Sensitivity.values:
            with self.subTest(level=level):
                if level in SAFE_SENSITIVITIES:
                    record = get_resource("documentation", f"example-{level}", principal=self.agent)
                    self.assertEqual(record["obsidian_path"], f"x/{level}.md")
                else:
                    with self.assertRaises(RecordNotFound):
                        get_resource("documentation", f"example-{level}", principal=self.agent)
        listed = list_resource("documentation", principal=self.agent)["items"]
        self.assertEqual({item["doc_id"] for item in listed}, {f"example-{level}" for level in SAFE_SENSITIVITIES})

    def test_a_person_reads_every_doc_whole(self):
        for level in DocumentationRecord.Sensitivity.values:
            with self.subTest(level=level):
                record = get_resource("documentation", f"example-{level}", principal=self.person)
                self.assertEqual(record["obsidian_path"], f"x/{level}.md")

    def test_an_answer_with_no_reader_blanks_an_unsafe_docs_pointers(self):
        sensitive = DocumentationRecord.objects.get(doc_id="example-sensitive")

        self.assertEqual(serialize_documentation(sensitive)["obsidian_path"], "")
        self.assertEqual(serialize_documentation(sensitive, principal=self.person)["obsidian_path"], "x/sensitive.md")
