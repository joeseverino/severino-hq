"""Every repository path a document names exists, so a reader who follows one finds the file."""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

from .source import source_files

ROOT = Path(__file__).resolve().parents[4]

# A path in backticks is written from the repository root, so its first part
# is one of the root's directories. Not var/, the gitignored output a task
# writes, and not a hidden directory: .github/ is also every extension's.
SPAN = re.compile(r"`([^`\s]+)`")
# A symbol or line after the path (`a.py:name`, `a.py::Test`).
AFTER = re.compile(r"::?[\w.:-]*$")
PLACEHOLDER = re.compile(r"[<>{}$]")
OUTPUT = "var"


def named_paths(text: str, roots: set[str]) -> list[tuple[int, str]]:
    """The repository paths ``text`` names in backticks, by line."""

    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for hit in SPAN.finditer(line):
            path = AFTER.sub("", hit.group(1))
            first = path.split("/", 1)[0]
            if "/" in path and first in roots and first != OUTPUT and not PLACEHOLDER.search(path):
                found.append((number, path))
    return found


def missing(root: Path, text: str) -> list[str]:
    roots = {each.name for each in root.iterdir() if each.is_dir() and not each.name.startswith(".")}
    return [
        f"{number}: {path}"
        for number, path in named_paths(text, roots)
        if not (any(root.glob(path)) if "*" in path else (root / path).exists())
    ]


@tag("source", "tree")
class DocumentPathTests(SimpleTestCase):
    def test_a_path_that_is_not_there_is_named(self):
        text = "Read `docs/Design.md`, `hq/nowhere.py:thing` and `tests/browser/widths.py`, in `var/gate/`."
        self.assertEqual(missing(ROOT, text), ["1: hq/nowhere.py", "1: tests/browser/widths.py"])

    def test_every_path_a_document_names_exists(self):
        documents = [path for path in source_files(ROOT, "*.md") if "vendor" not in path.parts]
        self.assertGreater(len(documents), 10)
        found = [
            f"{path.relative_to(ROOT)}:{line}"
            for path in documents
            for line in missing(ROOT, path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [], "a document names a path the repository does not hold; write it from the root")
