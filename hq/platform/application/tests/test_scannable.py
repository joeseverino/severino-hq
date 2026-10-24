"""Code scanning reads every clause of a comprehension HQ writes.

CodeQL's Python extractor parses a file its older parser cannot read, which is
any file with a `lazy` import, through tsg-python, and tsg-python loses a
comprehension's `for` or `if` clause when a comment line sits right before it.
The clause is then missing from the analysis: a name used only there is
reported unused, and a check it makes is invisible to the security queries.
A comment on a comprehension goes above its first `for`, or at the end of a
clause's line.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase, tag

REPO_ROOT = Path(__file__).resolve().parents[4]
SCANNED = ("hq", "hq_sdk", "tools", "demo", "tests")
COMPREHENSIONS = ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp


def hidden_clauses(source: str) -> list[int]:
    """The lines of the clauses after a comprehension's first `for` that a
    whole-line comment sits right before."""

    lines = source.splitlines()
    hidden = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, COMPREHENSIONS):
            first = node.generators[0].target.lineno
            starts = [clause.target.lineno for clause in node.generators[1:]]
            starts += [test.lineno for clause in node.generators for test in clause.ifs]
            hidden.update(at for at in starts if at - 1 > first and lines[at - 2].lstrip().startswith("#"))
    return sorted(hidden)


@tag("source", "tree")
class ScannableComprehensionTests(SimpleTestCase):
    def test_no_comment_sits_between_a_comprehensions_clauses(self):
        found = [
            f"{path.relative_to(REPO_ROOT)}:{line}"
            for package in SCANNED
            for path in sorted((REPO_ROOT / package).rglob("*.py"))
            for line in hidden_clauses(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [], "move each comment above the comprehension's first `for`")

    def test_a_comment_between_clauses_is_found(self):
        source = "[\n    x\n    for xs in xss\n    # why\n    for x in xs\n    # and why\n    if x\n]\n"
        self.assertEqual(hidden_clauses(source), [5, 7])

    def test_a_comment_above_the_first_for_or_ending_a_line_is_read(self):
        source = "[\n    x\n    # why\n    for x in xs\n    if x  # and why\n]\n"
        self.assertEqual(hidden_clauses(source), [])
