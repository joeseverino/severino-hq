"""One answer to which documentation an AI-facing surface may name."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

from docs_index.models import DocumentationRecord

from .sensitivity import SAFE_SENSITIVITIES


ROOT = Path(__file__).resolve().parent.parent


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

        from docs_index.models import SAFE_SENSITIVITIES as MODEL_SET

        self.assertIs(MODEL_SET, SAFE_SENSITIVITIES)

    def test_sensitive_and_restricted_documentation_is_never_safe(self):
        for level in (
            DocumentationRecord.Sensitivity.SENSITIVE,
            DocumentationRecord.Sensitivity.RESTRICTED,
        ):
            with self.subTest(level=level):
                self.assertNotIn(level, SAFE_SENSITIVITIES)

    def test_no_other_module_states_its_own(self):
        """A second copy is how one surface ends up wider than the rest."""

        offenders = []
        for path in sorted((ROOT / "application").rglob("*.py")):
            if path.name.startswith("test") or path.name == "sensitivity.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if any(isinstance(t, ast.Name) and t.id == "SAFE_SENSITIVITIES" for t in targets):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

        self.assertEqual(offenders, [])
