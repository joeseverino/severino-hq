"""One answer to which documentation an AI-facing surface may name."""

from django.test import SimpleTestCase

from hq.domains.docs_index.models import DocumentationRecord

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
