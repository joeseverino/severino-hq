"""Drift guards for the shared frontmatter schema.

The MCP's schema.py is canonical; HQ commits its JSON emission as schema.json
and validates the manifest against it. These tests make divergence fail loudly:

- ``test_model_choices_cover_canonical_schema`` — the DocumentationRecord choice
  members (HQ's symbolic API + admin labels) must cover exactly the canonical
  value sets. Runs everywhere; catches a model that lags the schema.
- ``test_committed_schema_matches_mcp`` — the committed schema.json must equal
  the installed MCP's current emission. Skipped where the MCP CLI isn't present
  (e.g. HQ's own container/CI); enforced on the dev machine and tools CI, where
  schema changes are actually authored.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from django.test import SimpleTestCase

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
                "installed severino-vault-mcp predates `schema` — run "
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
