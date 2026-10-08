"""docs/NEW_DOMAIN.md names every architecture rule and nothing that is gone."""

import ast
import re
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[4]
CHECKLIST = ROOT / "docs" / "NEW_DOMAIN.md"
RULE_FILES = ("test_architecture.py", "test_domains.py", "test_domain_declaration.py")
CITATION = re.compile(r"\b([A-Z]\w*Tests)\.(test_\w+)\b")


def rules() -> dict[str, set[str]]:
    """Test class -> its test methods, across the files that hold the rules."""

    found: dict[str, set[str]] = {}
    for name in RULE_FILES:
        tree = ast.parse((Path(__file__).parent / name).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            methods = {
                item.name for item in node.body if isinstance(item, ast.FunctionDef) and item.name.startswith("test_")
            }
            if methods:
                found[node.name] = methods
    return found


@skipUnless(CHECKLIST.exists(), "the docs are not in the image")
class NewDomainChecklistTests(SimpleTestCase):
    def setUp(self):
        self.text = CHECKLIST.read_text(encoding="utf-8")
        self.rules = rules()
        self.cited: dict[str, set[str]] = {}
        for cls, method in CITATION.findall(self.text):
            self.cited.setdefault(cls, set()).add(method)

    def test_the_rule_files_are_read(self):
        self.assertIn("DeliveryAdapterArchitectureTests", self.rules)
        self.assertIn("DomainRegistryTests", self.rules)
        self.assertIn("NewDomainDeclarationTests", self.rules)

    def test_every_rule_class_is_named(self):
        missing = [c for c in sorted(self.rules) if not re.search(rf"\b{c}\b", self.text)]
        self.assertEqual(missing, [], "name these in docs/NEW_DOMAIN.md")

    def test_a_class_the_steps_cite_is_cited_method_by_method(self):
        missing = [
            f"{cls}.{method}"
            for cls, cited in sorted(self.cited.items())
            if cls in self.rules
            for method in sorted(self.rules[cls] - cited)
        ]
        self.assertEqual(missing, [], "add these to the steps in docs/NEW_DOMAIN.md")

    def test_no_citation_names_a_rule_that_is_gone(self):
        gone = [
            f"{cls}.{method}"
            for cls, cited in sorted(self.cited.items())
            for method in sorted(cited - self.rules.get(cls, set()))
        ]
        self.assertEqual(gone, [], "remove these from docs/NEW_DOMAIN.md")
