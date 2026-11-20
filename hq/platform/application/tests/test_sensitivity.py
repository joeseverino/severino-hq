"""One answer to which documentation an AI-facing surface may name."""

from django.test import SimpleTestCase, TestCase

from hq.domains.docs_index.models import DocumentationRecord
from hq.platform.core.testing import operator

from ..documentation import serialize_documentation
from ..record_errors import RecordNotFound
from ..resources import get_resource, list_resource
from ..security import MCP_INTERFACE, Capability, Principal, web_principal

# Which sensitivities an AI-facing surface may name, read from the record itself.
SAFE = {
    value
    for value in DocumentationRecord.Sensitivity.values
    if DocumentationRecord(sensitivity=value).is_safe_for_ai_export
}


class SafeSensitivityTests(SimpleTestCase):
    def test_only_public_and_internal_documentation_is_safe(self):
        self.assertEqual(SAFE, {DocumentationRecord.Sensitivity.PUBLIC, DocumentationRecord.Sensitivity.INTERNAL})


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
                if level in SAFE:
                    record = get_resource("documentation", f"example-{level}", principal=self.agent)
                    self.assertEqual(record["obsidian_path"], f"x/{level}.md")
                else:
                    with self.assertRaises(RecordNotFound):
                        get_resource("documentation", f"example-{level}", principal=self.agent)
        listed = list_resource("documentation", principal=self.agent)["items"]
        self.assertEqual({item["doc_id"] for item in listed}, {f"example-{level}" for level in SAFE})

    def test_a_person_reads_every_doc_whole(self):
        for level in DocumentationRecord.Sensitivity.values:
            with self.subTest(level=level):
                record = get_resource("documentation", f"example-{level}", principal=self.person)
                self.assertEqual(record["obsidian_path"], f"x/{level}.md")

    def test_an_answer_with_no_reader_blanks_an_unsafe_docs_pointers(self):
        sensitive = DocumentationRecord.objects.get(doc_id="example-sensitive")

        self.assertEqual(serialize_documentation(sensitive)["obsidian_path"], "")
        self.assertEqual(serialize_documentation(sensitive, principal=self.person)["obsidian_path"], "x/sensitive.md")
