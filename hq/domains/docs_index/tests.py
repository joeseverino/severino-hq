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

from django.test import SimpleTestCase, TestCase

from . import frontmatter_schema
from .models import DocumentationRecord


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

    def test_status_field_covers_both_doc_and_task_statuses(self):
        # The status field holds either a standard doc status or a task lifecycle
        # status, so its choices must cover the union, otherwise admin/ModelForm
        # rejects a task's "open"/"done" that the importer legitimately writes.
        self.assertEqual(
            _values(DocumentationRecord.TaskStatus.choices),
            set(frontmatter_schema.TASK_STATUSES),
            "TaskStatus choices drifted from the schema's task_statuses.",
        )
        field_values = {value for value, _label in DocumentationRecord._meta.get_field("status").choices}
        self.assertEqual(
            field_values,
            set(frontmatter_schema.STATUSES) | set(frontmatter_schema.TASK_STATUSES),
            "The status field choices must cover STATUSES | TASK_STATUSES.",
        )


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
            self.skipTest(
                "installed severino-vault-mcp predates `schema`: run "
                "`site reinstall-mcp`"
            )
        emitted = json.loads(proc.stdout)
        committed = json.loads(
            frontmatter_schema.SCHEMA_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(
            emitted,
            committed,
            "docs_index/schema.json is stale. Regenerate it:\n"
            "  severino-vault-mcp schema --json > docs_index/schema.json",
        )


class RelatedDocumentsTests(TestCase):
    """A record's documents as its page lists them: by type, by title."""

    def _document(self, doc_id, title, doc_type="runbook", status="active"):
        return DocumentationRecord.objects.create(doc_id=doc_id, title=title, doc_type=doc_type, status=status)

    def test_groups_follow_the_type_order_and_tasks_come_last(self):
        from hq.platform.application.documentation import related_documents

        records = [
            self._document("task-example-done", "Ship it", "task", "done"),
            self._document("task-example-parked", "Wait for it", "task", "parked"),
            self._document("note-example-design", "Example design", "architecture_note"),
            self._document("rb-example-b", "Back up the example"),
            self._document("rb-example-a", "Add an example"),
        ]

        found = related_documents(records)

        self.assertEqual(
            [(group.label, [link.label for link in group.links], group.folded) for group in found.groups],
            [
                ("Runbooks", ["Add an example", "Back up the example"], False),
                ("Architecture notes", ["Example design"], False),
                ("Open tasks", ["Wait for it"], False),
                ("Done tasks", ["Ship it"], True),
            ],
        )
        self.assertEqual(found.groups[0].links[0].title, "rb-example-a")

    def test_nothing_related_is_falsy(self):
        from hq.platform.application.documentation import related_documents

        self.assertFalse(related_documents([]))

    def test_a_project_already_listed_is_not_listed_again(self):
        from hq.domains.projects.models import Project
        from hq.platform.application.documentation import related_documents

        project = Project.objects.create(name="Example Tool", slug="example-tool")
        note = self._document("project-example-tool", "Example Tool", "architecture_note")

        self.assertEqual([link.label for link in related_documents([note]).projects], ["Example Tool"])
        self.assertFalse(related_documents([note], listed=[project]))
        self.assertFalse(related_documents([note], about=project))
