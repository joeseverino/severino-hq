"""The primitives a Python string may not draw are the ones a script may not.

markup-drawn-by-templates (tools/lint/ast-grep) and ESLint's no-restricted-syntax
(tools/lint/eslint.config.mjs) refuse the same shared parts' classes, each in
its own tool's syntax, so a primitive added to one is added to both.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[4]
RULE = ROOT / "tools/lint/ast-grep/rules/markup-drawn-by-templates.yml"
ESLINT = ROOT / "tools/lint/eslint.config.mjs"


@tag("source")
class MarkupRuleTests(SimpleTestCase):
    def test_the_python_rule_refuses_the_classes_the_script_rule_does(self):
        import yaml  # The image's suite collects this module without PyYAML.

        declared = re.search(
            r"primitiveClass = String\.raw`/\(\^\|\\s\)(?P<names>\(.+\))\(\\s\|\$\)/`;",
            ESLINT.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(declared, f"{ESLINT.name} no longer declares primitiveClass, so this holds nothing")
        rule = yaml.safe_load(RULE.read_text(encoding="utf-8"))["rule"]
        classes = [each["regex"] for each in rule["any"] if "class=" in each["regex"]]
        self.assertEqual(len(classes), 1, f"{RULE.name} names the classes in one regex")
        self.assertIn(
            declared["names"],
            classes[0],
            f"{RULE.name}'s class names are primitiveClass's in {ESLINT.name}: copy them across",
        )
