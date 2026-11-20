"""What each test module ran: the record fast selects tests from (docs/Development.md, What `fast` runs).

A run of the suite under coverage.py keeps each line it runs under the test module running then (a dynamic
context, switched as each class sets up: hq/platform/core/run_report.py ``measuring``), and the record is
coverage's own database, read by line. Each function body a change reaches selects the test modules that
ran one of its lines, pytest-testmon's model. It is kept under the repository's common git directory,
named by the tree it was recorded on, so every worktree reads the one record.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path

from coverage import CoverageData

from tools.gate import chain
from tools.gate.names import functions, module_name
from tools.gate.tree import working_tree

# Where the record is kept, under the common git directory, a file named by its tree.
RECORDS = ("severino", "test-map")
# The task that records it.
TASK = "fast:map"


class Record:
    """The record, and the tree it was recorded on."""

    def __init__(self, tree: str, path: Path):
        self.tree = tree
        self.data = CoverageData(str(path), no_disk=False)
        self.data.read()
        self.files = {module_name(Path(f)): f for f in self.data.measured_files() if f.endswith(".py")}
        # The tests the record holds: a module none of whose classes ran is not one.
        self.tests = frozenset(self.data.measured_contexts() - {""})

    @property
    def modules(self) -> frozenset[str]:
        return frozenset(self.files)

    def ran(self, bodies: dict[str, set[str]], texts: dict[str, str]) -> set[str]:
        """The test modules that ran a line of one of ``bodies`` (module -> the function bodies' names),
        each body's lines read from the module's text at the record's tree: ``texts`` for one changed
        since, the file as it stands for any other."""

        found: set[str] = set()
        for module, names in bodies.items():
            for name, contexts in self.contexts(module, texts).items():
                if name in names:
                    found |= contexts
        return found - {""}

    def contexts(self, module: str, texts: dict[str, str]) -> dict[str, set[str]]:
        """Each function body of ``module`` (as ``ran`` reads its text), with the contexts that ran it."""

        path = self.files[module]
        text = texts.get(path)
        tree = ast.parse(text if text is not None else (chain.ROOT / path).read_text(encoding="utf-8"))
        lines = self.data.contexts_by_lineno(path)
        return {
            name: set().union(
                *(lines.get(n, ()) for n in range(function.body[0].lineno, (function.end_lineno or 0) + 1))
            )
            for name, function in functions(tree.body)
        }

    def started(self, texts: dict[str, str]) -> Started:
        return Started(self, texts)


class Started(Mapping[str, frozenset[str]]):
    """Each module the record holds, with the function bodies that ran as the suite started, before any test
    (a registry's assembly): what runs as the app starts is part of its loading, which a test that never
    runs it again still reads, so tools/gate/names.py ``name_reach`` walks it as it walks a module's loading.
    Read as the walk asks for a module."""

    def __init__(self, record: Record, texts: dict[str, str]):
        self.record, self.texts = record, texts
        self.started = cache(self._started)

    def _started(self, module: str) -> frozenset[str]:
        return frozenset(name for name, contexts in self.record.contexts(module, self.texts).items() if "" in contexts)

    def __getitem__(self, module: str) -> frozenset[str]:
        if module not in self.record.files:
            raise KeyError(module)
        return self.started(module)

    def __iter__(self) -> Iterator[str]:
        return iter(self.record.files)

    def __len__(self) -> int:
        return len(self.record.files)


def said(record: Record | None, since: int) -> str:
    """What fast's line on its selection says of the record it selected by."""

    if record is None:
        return f"no record of what each test ran, so the reach is read from the source alone: `mise run {TASK}`"
    return (
        f"selected by the record of tree {record.tree[:9]}, {since} files changed since (`mise run {TASK}` renews it)"
    )


def records(root: Path = chain.ROOT) -> Path:
    return chain.passes(root).parent.joinpath(*RECORDS[1:])


def recorded(root: Path = chain.ROOT) -> Record | None:
    """The record, when one is kept and git still holds its tree."""

    folder = records(root)
    kept = sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime) if folder.is_dir() else []
    if not kept:
        return None
    tree = kept[-1].name
    held = subprocess.run(["git", "cat-file", "-e", f"{tree}^{{tree}}"], cwd=root, capture_output=True, check=False)
    return Record(tree, kept[-1]) if held.returncode == 0 else None


def keep(data: Path, tree: str, root: Path = chain.ROOT) -> Path:
    """Keep ``data`` as the record of ``tree``, in place of any record before it."""

    folder = records(root)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        old.unlink()
    kept = folder / tree
    data.replace(kept)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", action="store_true", help="print the tree of the checkout as its files stand")
    parser.add_argument("--keep", nargs=2, metavar=("DATA", "TREE"), help="keep DATA as the record of TREE")
    asked = parser.parse_args()
    if asked.tree:
        print(working_tree(chain.ROOT))
    elif asked.keep:
        data, tree = asked.keep
        kept = Record(tree, keep(Path(data), tree))
        print(f"recorded what {len(kept.tests)} test modules ran, on tree {tree[:9]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
