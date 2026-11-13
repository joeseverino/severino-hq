"""Which test modules ran each Python file, read from a whole run's coverage.

fast's whole-suite run records it, as pytest-testmon does: coverage measures
the run with its C tracer (``sys.monitoring`` records a line once per process,
whatever the context), and the runner switches the context to each test
module before the module's fixtures run (``hq/platform/core/coverage_contexts.py``),
so each line is kept once for every test module that ran it. The map is kept
by the tree that run passed on, beside the record of passed trees.

A later run reads it for a file whose change from that tree stays inside
function bodies (``bodies_only``). Such a change does nothing at import, so
only a test that runs the function sees it; a test that ran none of the file
then runs the same code now as it did at the map's tree until it reaches a
file changed since, which selects it in turn.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# Only a run that reads a recording loads coverage.
lazy from coverage import CoverageData

# Where the recording run keeps coverage's data, apart from the suite's own
# (`mise run test:suite`), so neither run combines the other's shards.
DATA = Path("var", "coverage", "map", "data")
# Beside fast-passes, under the common git directory every worktree shares.
MAPS = "fast-maps"
# How many maps are kept: the newest, and those a branch made a little earlier
# may still read from.
KEPT = 4


class TestMap(NamedTuple):
    """The test modules the recording ran, and each file's test modules, by its path from the root."""

    ran: frozenset[str]
    files: dict[str, frozenset[str]]

    def tests_of(self, path: str) -> frozenset[str]:
        return self.files.get(path, frozenset())


def read(data_file: Path, root: Path) -> TestMap:
    """The map in a recording's data: its shards combined, each measured file
    under ``root`` with the test modules whose context ran any of its lines.
    A line run outside every test module's context (an import as the suite
    loads, a worker starting) is no test's."""

    environment = {**os.environ, "COVERAGE_FILE": str(data_file)}
    subprocess.run([sys.executable, "-m", "coverage", "combine", "--quiet"], cwd=root, env=environment, check=True)
    data = CoverageData(basename=str(data_file))
    data.read()
    files: dict[str, frozenset[str]] = {}
    for measured in data.measured_files():
        path = Path(measured)
        if not path.is_relative_to(root):
            continue
        modules = frozenset().union(*data.contexts_by_lineno(measured).values()) - {""}
        if modules:
            files[path.relative_to(root).as_posix()] = modules
    return TestMap(frozenset(data.measured_contexts()) - {""}, files)


def save(folder: Path, tree: str, found: TestMap) -> Path:
    """Keep ``found`` as the map of ``tree``, and only the newest KEPT maps."""

    tests = sorted(found.ran | {module for modules in found.files.values() for module in modules})
    index = {module: n for n, module in enumerate(tests)}
    body = {
        "ran": sorted(index[module] for module in found.ran),
        "tests": tests,
        "files": {path: sorted(index[module] for module in modules) for path, modules in sorted(found.files.items())},
    }
    folder.mkdir(parents=True, exist_ok=True)
    kept = folder / f"{tree}.json"
    written = kept.with_suffix(".part")
    written.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    written.replace(kept)
    for old in sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[KEPT:]:
        old.unlink(missing_ok=True)
    return kept


def load(path: Path) -> TestMap:
    body = json.loads(path.read_text(encoding="utf-8"))
    tests = body["tests"]
    files = {name: frozenset(tests[n] for n in modules) for name, modules in body["files"].items()}
    return TestMap(frozenset(tests[n] for n in body["ran"]), files)


def bodies_only(before: str, after: str) -> bool:
    """Whether two versions of a module differ only inside function bodies,
    a function's docstring aside, which it is given as it is defined and
    which HQ reads (API and MCP descriptions)."""

    try:
        return skeleton(before) == skeleton(after)
    except SyntaxError:
        return False


def skeleton(source: str) -> str:
    """What a module runs as it is imported: its tree with each function's
    body past its docstring left out."""

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            kept = node.body[:1] if ast.get_docstring(node, clean=False) is not None else []
            node.body = [*kept, ast.Pass()]
    return ast.dump(tree, include_attributes=False)
