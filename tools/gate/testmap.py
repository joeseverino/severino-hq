"""Which test modules ran each line of each Python file, read from a whole run's coverage.

CI's Map job records it (`mise run fast:map`): coverage measures the run with
its C tracer (``sys.monitoring`` records a line once per process, whatever the
context), and the runner switches the context to each test module before the
module's fixtures run and to each test before it runs
(``hq/platform/core/coverage_contexts.py``), so each line is kept once for every
test that ran it, and read here by its module. The map is coverage's own
record, ``contexts_by_lineno``, named by the commit it was recorded at, whose
lines it numbers.

fast reads it for a file whose change from that commit stays inside function
bodies (``touched_bodies``): the tests that ran the functions it touched. Such a
change does nothing at import, so only a test that runs one of those functions
sees it; a test that ran none of them runs the same code now as it did at the
map's commit until it reaches a file changed since, which selects it in turn.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# Only a run that reads a recording loads coverage.
lazy from coverage import CoverageData

from hq.platform.core.coverage_contexts import module_of

# Where the recording run keeps coverage's data, apart from the suite's own
# (`mise run test:suite`), so neither run combines the other's shards, and the map it writes from it.
DATA = Path("var", "coverage", "map", "data")
WRITTEN = Path("var", "coverage", "map", "map.json")
# Beside fast-passes, under the common git directory every worktree shares: a
# map fetched from CI, named by its commit.
MAPS = "fast-line-maps"
# How many maps are kept: the newest, and those a branch made a little earlier
# may still read from.
KEPT = 4


class TestMap(NamedTuple):
    """The test modules the recording ran, and for each file by its path from the root, each line's."""

    ran: frozenset[str]
    lines: dict[str, dict[int, frozenset[str]]]

    def tests_of(self, path: str, lines: set[int] | None = None) -> frozenset[str]:
        """The test modules that ran ``lines`` of ``path``, or any of it."""

        held = self.lines.get(path, {})
        return frozenset().union(*(modules for line, modules in held.items() if lines is None or line in lines))


def combined(data_file: Path, root: Path) -> CoverageData:
    """A recording's data, its shards combined into it first."""

    if any(data_file.parent.glob(f"{data_file.name}.*")):
        environment = {**os.environ, "COVERAGE_FILE": str(data_file)}
        subprocess.run([sys.executable, "-m", "coverage", "combine", "--quiet"], cwd=root, env=environment, check=True)
    data = CoverageData(basename=str(data_file))
    data.read()
    return data


def read(data: CoverageData, root: Path) -> TestMap:
    """The map in a recording's data: each measured line under ``root`` with
    the test modules whose fixtures or tests ran it. A line run outside every
    test's and module's context (an import as the suite loads, a worker
    starting) is no test's."""

    lines: dict[str, dict[int, frozenset[str]]] = {}
    for measured in data.measured_files():
        path = Path(measured)
        if not path.is_relative_to(root):
            continue
        held = {
            line: frozenset(map(module_of, contexts)) - {""}
            for line, contexts in data.contexts_by_lineno(measured).items()
            if set(contexts) - {""}
        }
        if held:
            lines[path.relative_to(root).as_posix()] = held
    return TestMap(frozenset(map(module_of, data.measured_contexts())) - {""}, lines)


def kept(folder: Path, commit: str) -> Path:
    return folder / f"{commit}.json"


def keep(folder: Path, commit: str, body: bytes) -> Path:
    """Keep ``body``, a map as ``write`` writes it, as the map of ``commit``, and only the newest KEPT maps."""

    folder.mkdir(parents=True, exist_ok=True)
    written = kept(folder, commit).with_suffix(".part")
    written.write_bytes(body)
    written.replace(kept(folder, commit))
    for old in sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[KEPT:]:
        old.unlink(missing_ok=True)
    return kept(folder, commit)


def write(kept: Path, found: TestMap) -> None:
    """``found`` as JSON at ``kept``: the test modules once, each distinct set of them once, and each
    line by the set it holds, since most lines of a function share one."""

    held = {line_set for lines in found.lines.values() for line_set in lines.values()}
    tests = sorted(found.ran | {module for modules in held for module in modules})
    index = {module: n for n, module in enumerate(tests)}
    sets = sorted({tuple(sorted(index[module] for module in modules)) for modules in held})
    numbered = {modules: n for n, modules in enumerate(sets)}
    body = {
        "ran": sorted(index[module] for module in found.ran),
        "tests": tests,
        "sets": sets,
        "lines": {
            path: {
                str(line): numbered[tuple(sorted(index[module] for module in modules))]
                for line, modules in sorted(lines.items())
            }
            for path, lines in sorted(found.lines.items())
        },
    }
    kept.parent.mkdir(parents=True, exist_ok=True)
    written = kept.with_suffix(".part")
    written.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    written.replace(kept)


def load(path: Path) -> TestMap:
    body = json.loads(path.read_text(encoding="utf-8"))
    tests = body["tests"]
    sets = [frozenset(tests[n] for n in modules) for modules in body["sets"]]
    lines = {name: {int(line): sets[n] for line, n in held.items()} for name, held in body["lines"].items()}
    return TestMap(frozenset(tests[n] for n in body["ran"]), lines)


# A hunk's header in a diff of no context lines: where it starts in the old file and how many lines it holds
# there, and the same of the new file.
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def touched(diff: str) -> tuple[set[int], set[int]]:
    """(the lines of the old file a diff of no context, ``git diff -U0``, touches, the lines of the new
    file it adds). A hunk that only adds touches the old line it is added before: git numbers such a
    hunk by the line it follows."""

    old: set[int] = set()
    new: set[int] = set()
    for start, count, added, many in HUNK.findall(diff):
        first, removed = int(start), 1 if count == "" else int(count)
        old |= set(range(first, first + removed)) if removed else {first + 1}
        new |= set(range(int(added), int(added) + (1 if many == "" else int(many))))
    return old, new


def touched_bodies(source: str, lines: set[int]) -> set[int] | None:
    """Every line of each function body in ``source`` that ``lines`` fall in, the innermost function's, or
    None when one falls outside every body (a module-level line, a signature, a decorator, a class's own
    body) or on a function's docstring, which it is given as it is defined and which HQ reads (API and
    MCP descriptions): a change there acts at import, which a test's context does not see."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    bodies = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body[1:] if ast.get_docstring(node, clean=False) is not None else node.body
            if body:
                bodies.append((body[0].lineno, body[-1].end_lineno or body[-1].lineno))
    widened: set[int] = set()
    for line in lines:
        holding = [(first, last) for first, last in bodies if first <= line <= last]
        if not holding:
            return None
        first, last = min(holding, key=lambda span: span[1] - span[0])
        widened |= set(range(first, last + 1))
    return widened


if __name__ == "__main__":
    # `python -m tools.gate.testmap`, which fast:map runs after the recording: its data, as the map.
    root = Path.cwd()
    found = read(combined(root / DATA, root), root)
    write(root / WRITTEN, found)
    print(f"the map of the tests that ran each line: {len(found.lines)} files, {len(found.ran)} test modules")
