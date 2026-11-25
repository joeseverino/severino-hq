"""Each file rule refuses its cases and passes the rest, and reads a file by the name a rule holds."""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from ..file_rules import RULES, violations


class FileRuleTests(SimpleTestCase):
    def test_each_rule_refuses_its_cases_and_passes_the_rest(self):
        for key, rule in RULES.items():
            for case in rule.refuses:
                with self.subTest(key, refuses=case):
                    self.assertTrue(list(rule.finds(case)))
            for case in rule.passes:
                with self.subTest(key, passes=case):
                    self.assertEqual(list(rule.finds(case)), [])

    def test_a_template_is_named_as_django_loads_it_and_its_owner_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partials = root / "example" / "templates" / "partials"
            partials.mkdir(parents=True)
            written = "{% for error in form.non_field_errors %}{{ error }}{% endfor %}\n"
            (partials / "_form_errors.html").write_text(written)
            (partials / "_example.html").write_text("\n" + written)
            vendored = root / "static" / "vendor" / "example.html"
            vendored.parent.mkdir(parents=True)
            vendored.write_text(written)

            found = {key: [line.partition(": ")[0] for line in lines] for key, lines in violations(root).items()}

            self.assertEqual(found, {"form-errors": ["example/templates/partials/_example.html:2"]})
            self.assertEqual(violations(root, partials / "_form_errors.html"), {})
