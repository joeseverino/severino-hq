"""Drift guards for the shared frontmatter schema.

The MCP's schema.py is canonical; HQ commits its JSON emission as schema.json
and validates the manifest against it. These tests make divergence fail loudly:

- ``test_model_choices_cover_canonical_schema``: the DocumentationRecord choice
  members (HQ's symbolic API + admin labels) must cover exactly the canonical
  value sets. Runs everywhere; catches a model that lags the schema.
- ``test_committed_schema_matches_mcp``: the committed schema.json must equal
  the installed MCP's current emission. Skipped where the MCP CLI isn't present
  (e.g. HQ's own container/CI); enforced on the dev machine and tools CI, where
  schema changes are actually authored.
"""

import json
import shutil
import subprocess

from django.test import SimpleTestCase, TestCase, tag

from .. import frontmatter_schema
from ..models import DocumentationRecord


def _values(choices) -> set[str]:
    return {value for value, _label in choices}


class ModelChoicesMatchSchemaTests(SimpleTestCase):
    def test_model_choices_cover_canonical_schema(self) -> None:
        cases = {
            "doc_type": (
                _values(DocumentationRecord.DocType.choices),
                frontmatter_schema.DOC_TYPES,
            ),
            "environment": (
                _values(DocumentationRecord.Environment.choices),
                frontmatter_schema.ENVIRONMENTS,
            ),
            "status": (
                _values(DocumentationRecord.Status.choices),
                frontmatter_schema.STATUSES,
            ),
            "sensitivity": (
                _values(DocumentationRecord.Sensitivity.choices),
                frontmatter_schema.SENSITIVITIES,
            ),
        }
        for field, (model_values, schema_values) in cases.items():
            self.assertEqual(
                model_values,
                set(schema_values),
                f"DocumentationRecord {field} choices have drifted from the "
                f"canonical schema. Update the TextChoices to match "
                f"docs_index/schema.json.",
            )


@tag("source")
class CommittedSchemaMatchesMcpTests(SimpleTestCase):
    def test_committed_schema_matches_mcp(self) -> None:
        cli = shutil.which("severino-vault-mcp")
        if not cli:
            self.skipTest("severino-vault-mcp not on PATH")
        proc = subprocess.run(
            [cli, "schema", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            self.skipTest("installed severino-vault-mcp predates `schema`: run `site reinstall-mcp`")
        emitted = json.loads(proc.stdout)
        committed = json.loads(frontmatter_schema.SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            emitted,
            committed,
            "docs_index/schema.json is stale. Regenerate it:\n"
            "  severino-vault-mcp schema --json > docs_index/schema.json",
        )


class ReviewQueueTests(TestCase):
    def test_a_frozen_report_and_a_generated_queue_note_are_never_queued_for_review_but_a_runbook_is(self):
        DocumentationRecord.objects.create(
            doc_id="report-example", title="Example report", doc_type="report", status="active"
        )
        DocumentationRecord.objects.create(
            doc_id="queue-example", title="Example queue", doc_type="queue", status="active"
        )
        DocumentationRecord.objects.create(
            doc_id="rb-example", title="Example runbook", doc_type="runbook", status="active"
        )

        queued = DocumentationRecord.objects.needing_review().values_list("doc_id", flat=True)

        self.assertEqual(list(queued), ["rb-example"])


class RelatedDocumentsTests(TestCase):
    """A record's documents as its page lists them: by type, by title."""

    def _document(self, doc_id, title, doc_type="runbook", status="active"):
        return DocumentationRecord.objects.create(doc_id=doc_id, title=title, doc_type=doc_type, status=status)

    def test_groups_follow_the_type_order(self):
        from hq.platform.application.documentation import related_documents

        records = [
            self._document("note-example-design", "Example design", "architecture_note"),
            self._document("rb-example-b", "Back up the example"),
            self._document("rb-example-a", "Add an example"),
        ]

        found = related_documents(records)

        self.assertEqual(
            [(group.heading, [item.link.label for item in group.items]) for group in found.groups],
            [
                ("Runbooks", ["Add an example", "Back up the example"]),
                ("Architecture notes", ["Example design"]),
            ],
        )
        self.assertEqual(found.groups[0].items[0].link.title, "rb-example-a")

    def test_a_project_already_listed_is_not_listed_again(self):
        from hq.domains.projects.models import Project
        from hq.platform.application.documentation import related_documents

        project = Project.objects.create(name="Example Tool", slug="example-tool")
        note = self._document("project-example-tool", "Example Tool", "architecture_note")

        self.assertEqual([link.label for link in related_documents([note]).projects], ["Example Tool"])
        self.assertEqual(related_documents([note], listed=[project]).projects, ())
        self.assertEqual(related_documents([note], about=project).projects, ())
