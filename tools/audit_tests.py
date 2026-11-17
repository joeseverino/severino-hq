"""audit:tests: the tests whose covered lines other tests near them also cover.

It reads the recording CI's Map job makes (`mise run fast:map`), where each
test's lines are its own coverage context (``hq/platform/core/coverage_contexts.py``),
through coverage's own ``CoverageData``, and finds the code each test reaches
through coverage's own ``code_regions``. A test's peers are the tests of its
module, or of its class in a module of more than ``LARGE`` tests, so a page
crawl elsewhere in its app never covers a unit test. Each test names its peers
whose lines are a superset of its own (``wider``), the tests of its app or
package (its family) whose lines are the same (``equal``), and whether any line
it ran is run by no other test at all. A test is grouped under the code it
reaches: of the functions it ran, the one the fewest tests run.

Lines are compared without the tests' own files, which coverage omits, and
without what a module's class fixtures run, which is the module's, not a
test's. A test's size is its lines past its setup: those every one of its
peers runs too, such as a ``setUp`` and the imports it triggers (every test
of its module, or else of its family, where its peers all run the same lines).
Candidates and groups of equal tests are ranked by it.

Two kinds of test are left out, as covering and as covered, since the lines
they run are incidental: one tagged ``source`` or ``tree``, which reads the
repository's files, and one whose family's own files the recording holds none
of, as coverage omits ``tools/``. A test that ran no measured line is absent
from the recording.
"""

from __future__ import annotations

import ast
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

from coverage import CoverageData
from coverage.regions import code_regions

from hq.platform.core.coverage_contexts import SEPARATOR, label_of, module_of
from tools.gate import testmap

WRITTEN = Path("var", "audit", "tests", "report.json")
CAVEAT = (
    "Coverage is a candidate, not proof: a test whose lines another covers may assert an outcome the other "
    "never checks. Each entry is a test to read beside the ones named, never a verdict to delete it."
)
# The tags of tests that read files rather than run the product (hq/platform/core/test_runner.py).
LEFT_OUT = frozenset({"source", "tree"})
TAGGED = "tagged source or tree"
UNMEASURED = "their family's own code unmeasured"
# A module of more tests than this compares each test with its class alone: such a module mixes a crawl
# with the unit tests beside it.
LARGE = 40
# Rows in each table of the summary; the report holds them all.
SHOWN = 15


class Coverage(NamedTuple):
    """Each test's lines as one bit each, the one function of each test's lines the fewest tests run, how
    many lines were measured, and the tests left out, by why."""

    lines: dict[str, int]
    reaches: dict[str, str]
    measured: int
    left_out: dict[str, list[str]]


def family(module: str) -> str:
    """The app or package a test module tests: what stands above its ``tests`` package, or its own package."""

    parts = module.split(".")
    if "tests" in parts:
        return ".".join(parts[: parts.index("tests")]) or "tests"
    return module.rpartition(".")[0] or module


def class_of(test: str) -> str:
    """The class a test's context names, with its module: ``module:Class``."""

    return test.rpartition(".")[0]


class Tags:
    """The tags Django's ``@tag`` gives each test, read from its module's source: those on its method, its
    class and every class it inherits from, as the decorator unites them. A base is followed by its name,
    in its module or through a ``from`` import, and a module that does not parse holds no tags."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.parsed: dict[str, tuple[dict[str, ast.ClassDef], dict[str, tuple[str, str]]]] = {}

    def module(self, name: str) -> tuple[dict[str, ast.ClassDef], dict[str, tuple[str, str]]]:
        if name not in self.parsed:
            classes: dict[str, ast.ClassDef] = {}
            imported: dict[str, tuple[str, str]] = {}
            try:
                tree = ast.parse((self.root / (name.replace(".", "/") + ".py")).read_text(encoding="utf-8"))
            except OSError, SyntaxError, UnicodeDecodeError:
                tree = ast.Module(body=[], type_ignores=[])
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    classes[node.name] = node
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    imported.update({alias.asname or alias.name: (node.module, alias.name) for alias in node.names})
            self.parsed[name] = classes, imported
        return self.parsed[name]

    def lineage(self, module: str, qualname: str) -> list[tuple[str, ast.ClassDef]]:
        """The class ``qualname`` of ``module`` and each class it inherits from that the source names, nearest first."""

        found: list[tuple[str, ast.ClassDef]] = []
        seen: set[tuple[str, str]] = set()

        def visit(where: str, name: str) -> None:
            classes, imported = self.module(where)
            first, _, rest = name.partition(".")
            if first not in classes:
                if first in imported and not rest:
                    visit(*imported[first])
                return
            node = classes[first]
            for part in filter(None, rest.split(".")):
                node = next((n for n in node.body if isinstance(n, ast.ClassDef) and n.name == part), None)
                if node is None:
                    return
            if (where, name) in seen:
                return
            seen.add((where, name))
            found.append((where, node))
            for base in node.bases:
                if isinstance(base, ast.Name):
                    visit(where, base.id)

        visit(module, qualname)
        return found

    def of(self, test: str) -> frozenset[str]:
        module, _, rest = test.partition(SEPARATOR)
        qualname, _, method = rest.rpartition(".")
        lineage = self.lineage(module, qualname)
        held = {tag for _, node in lineage for tag in tags(node.decorator_list)}
        for _, node in lineage:
            defined = [
                each
                for each in node.body
                if isinstance(each, (ast.FunctionDef, ast.AsyncFunctionDef)) and each.name == method
            ]
            if defined:
                held.update(tags(defined[0].decorator_list))
                break
        return frozenset(held)


def tags(decorators: list[ast.expr]) -> set[str]:
    """The words of each ``@tag(...)`` among ``decorators``."""

    return {
        argument.value
        for decorator in decorators
        if isinstance(decorator, ast.Call)
        and (decorator.func.id if isinstance(decorator.func, ast.Name) else getattr(decorator.func, "attr", None))
        == "tag"
        for argument in decorator.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    }


def functions(source: str) -> dict[int, str]:
    """Each line of ``source`` with the innermost function it stands in, as coverage's regions name it."""

    held: dict[int, str] = {}
    for region in code_regions(source):
        if region.kind == "function":
            held.update(dict.fromkeys(region.lines, region.name))
    return held


def reach(test: str, ran: set[str], run_by: dict[str, set[str]]) -> str:
    """Of the functions a test ran, the one the fewest tests run, its own family's code before any other's:
    what the test is about, rather than a cache another test happened to fill first."""

    own = family(module_of(test)).replace(".", "/") + "/"
    held = [function for function in ran if function.startswith(own)] or ran
    return min(held, key=lambda function: (len(run_by[function]), function))


def measure(data: CoverageData, root: Path) -> Coverage:
    files = [Path(each) for each in sorted(data.measured_files()) if Path(each).is_relative_to(root)]
    found_tags = Tags(root)
    held = {path.relative_to(root).as_posix() for path in files}
    measured_families: dict[str, bool] = {}
    left_out: dict[str, list[str]] = {TAGGED: [], UNMEASURED: []}
    for context in sorted(data.measured_contexts()):
        if SEPARATOR not in context:
            continue
        own = family(module_of(context))
        if own not in measured_families:
            prefix = own.replace(".", "/") + "/"
            measured_families[own] = any(path.startswith(prefix) for path in held)
        if found_tags.of(context) & LEFT_OUT:
            left_out[TAGGED].append(context)
        elif not measured_families[own]:
            left_out[UNMEASURED].append(context)
    skipped = {context for contexts in left_out.values() for context in contexts}
    # Each line any context ran, numbered, as one bit of each test's lines.
    numbered: dict[tuple[Path, int], int] = {}
    for path in files:
        for line in sorted(data.lines(str(path)) or ()):
            numbered[path, line] = len(numbered)
    measured = len(numbered)
    bits: dict[str, bytearray] = defaultdict(lambda: bytearray(measured // 8 + 1))
    run_by: dict[str, set[str]] = defaultdict(set)
    ran: dict[str, set[str]] = defaultdict(set)
    for path in files:
        try:
            names = functions(path.read_text(encoding="utf-8"))
        except OSError, SyntaxError, UnicodeDecodeError:
            names = {}
        where = path.relative_to(root).as_posix()
        for line, contexts in data.contexts_by_lineno(str(path)).items():
            tests = [context for context in contexts if SEPARATOR in context and context not in skipped]
            bit = numbered[path, line]
            for test in tests:
                bits[test][bit >> 3] |= 1 << (bit & 7)
            if line in names:
                function = f"{where}:{names[line]}"
                run_by[function].update(tests)
                for test in tests:
                    ran[test].add(function)
    reaches = {test: reach(test, held, run_by) for test, held in ran.items()}
    lines = {test: int.from_bytes(held, "little") for test, held in bits.items()}
    return Coverage(
        lines, reaches, measured, {why: [label_of(test) for test in tests] for why, tests in left_out.items()}
    )


def every(levels: list[list[str]], lines: dict[str, int]) -> int:
    """The lines each test of the first of ``levels`` whose tests run more than one set of lines runs: its
    setup. Where every test runs the same lines, nothing tells its setup from the code it is about."""

    for tests in levels:
        held = {lines[test] for test in tests}
        if len(held) > 1:
            shared = held.pop()
            for each in held:
                shared &= each
            return shared
    return 0


def audit(found: Coverage) -> dict:
    lines = found.lines
    once = twice = 0
    for held in lines.values():
        twice |= once & held
        once |= held
    alone = once & ~twice
    by_family: dict[str, list[str]] = defaultdict(list)
    by_module: dict[str, list[str]] = defaultdict(list)
    by_class: dict[str, list[str]] = defaultdict(list)
    for test in sorted(lines):
        by_family[family(module_of(test))].append(test)
        by_module[module_of(test)].append(test)
        by_class[class_of(test)].append(test)
    scopes = [
        group
        for module, tests in by_module.items()
        for group in (
            [by_class[name] for name in dict.fromkeys(map(class_of, tests))] if len(tests) > LARGE else [tests]
        )
    ]
    setup: dict[str, int] = {}
    wider: dict[str, list[str]] = {}
    for tests in scopes:
        module = module_of(tests[0])
        shared = every([tests, by_module[module], by_family[family(module)]], lines)
        # The tests that run each one set of lines, and the sets by size, so only a larger set can hold one.
        sharing: dict[int, list[str]] = defaultdict(list)
        for test in tests:
            setup[test] = shared
            sharing[lines[test]].append(test)
        held = sorted((mine.bit_count(), mine) for mine in sharing)
        for n, (size, mine) in enumerate(held):
            covering = [
                test
                for other_size, other in held[n + 1 :]
                if other_size > size and other & mine == mine
                for test in sharing[other]
            ]
            if covering:
                wider.update(dict.fromkeys(sharing[mine], covering))
    past = {test: (lines[test] & ~setup[test]).bit_count() for test in lines}
    equal: dict[str, list[str]] = {}
    groups = []
    for tests in by_family.values():
        sharing = defaultdict(list)
        for test in tests:
            sharing[lines[test]].append(test)
        for mine, same in sharing.items():
            if len(same) > 1:
                equal.update({test: [other for other in same if other != test] for test in same})
                groups.append(
                    {
                        "lines": mine.bit_count(),
                        "past setup": min(past[test] for test in same),
                        "tests": [label_of(test) for test in same],
                    }
                )
    groups.sort(key=lambda group: (-group["past setup"], -group["lines"], group["tests"]))
    candidates = sorted(set(equal) | set(wider), key=lambda test: (-past[test], test))
    rows = [
        {
            "test": label_of(test),
            "family": family(module_of(test)),
            "lines": lines[test].bit_count(),
            "past setup": past[test],
            "reaches": found.reaches.get(test, ""),
            "equal": sorted(map(label_of, equal.get(test, ()))),
            # The nearest first: the wider test that runs the fewest lines besides.
            "wider": [
                label_of(other)
                for other in sorted(wider.get(test, ()), key=lambda other: (lines[other].bit_count(), other))
            ],
        }
        for test in candidates
    ]
    shared_only = sorted(test for test, held in lines.items() if not held & alone)
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[row["reaches"]].append(row["test"])
    named = Counter(row["family"] for row in rows)
    sharing_by_family = Counter(family(module_of(test)) for test in shared_only)
    return {
        "caveat": CAVEAT,
        "counts": {
            "tests": len(lines),
            **{f"left out, {why}": len(tests) for why, tests in found.left_out.items()},
            "lines run": found.measured,
            "candidates": len(rows),
            "candidates equal to another": len(equal),
            "groups of equal tests": len(groups),
            "median candidate's lines past setup": int(statistics.median(past[test] for test in candidates))
            if candidates
            else 0,
            "without a line of their own": len(shared_only),
        },
        "families": sorted(
            (
                {
                    "family": name,
                    "tests": len(tests),
                    "candidates": named[name],
                    "without a line of their own": sharing_by_family[name],
                }
                for name, tests in by_family.items()
            ),
            key=lambda row: (-row["candidates"], row["family"]),
        ),
        "equal": groups,
        "reaches": sorted(
            ({"reaches": reach, "candidates": tests} for reach, tests in grouped.items()),
            key=lambda row: (-len(row["candidates"]), row["reaches"]),
        ),
        "candidates": rows,
        "without a line of their own": [label_of(test) for test in shared_only],
        "left out": found.left_out,
    }


def summary(report: dict, written: Path) -> str:
    counts = report["counts"]
    out = [report["caveat"], ""]
    out += [f"{value:>8}  {name}" for name, value in counts.items()]
    out += ["", "Families by candidates (tests, candidates, without a line of their own):"]
    out += [
        f"{row['tests']:>8} {row['candidates']:>6} {row['without a line of their own']:>6}  {row['family']}"
        for row in report["families"][:SHOWN]
    ]
    out += ["", "Equal tests by lines past setup (past setup, lines, tests):"]
    out += [
        f"{group['past setup']:>8} {group['lines']:>6} {len(group['tests']):>6}  {group['tests'][0]}"
        for group in report["equal"][:SHOWN]
    ]
    out += ["", "Code reached by the most candidates:"]
    out += [f"{len(row['candidates']):>8}  {row['reaches'] or '(no function)'}" for row in report["reaches"][:SHOWN]]
    out += ["", f"Every row: {written}"]
    return "\n".join(out)


def main(data_file: Path, root: Path) -> None:
    if any(data_file.parent.glob(f"{data_file.name}.*")):
        sys.exit(f"{data_file} is in shards: `uv run python -m tools.gate.testmap`, fast:map's reader, combines them")
    if not data_file.is_file():
        sys.exit(f"no recording at {data_file}: CI's Map job makes one (`mise run fast:map`)")
    data = CoverageData(basename=str(data_file))
    data.read()
    report = audit(measure(data, root))
    written = root / WRITTEN
    written.parent.mkdir(parents=True, exist_ok=True)
    partial = written.with_suffix(".part")
    partial.write_text(json.dumps(report, indent=1), encoding="utf-8")
    partial.replace(written)
    print(summary(report, WRITTEN))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else testmap.DATA, Path.cwd())
