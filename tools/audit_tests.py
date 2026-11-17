"""audit:tests: the tests whose covered lines other tests of their app or package also cover.

It reads the recording CI's Map job makes (`mise run fast:map`), where each
test's lines are its own coverage context (``hq/platform/core/coverage_contexts.py``),
through coverage's own ``CoverageData``, and finds the code each test reaches
through coverage's own ``code_regions``. For each test it names every other
test of its family (the app or package above its ``tests`` package) whose lines
are a superset of its own, and whether any line it ran is run by no other test
at all. A test is grouped under the code it reaches: of the functions it ran,
the one the fewest tests run.

Lines are compared without the tests' own files, which coverage omits, and
without what a module's class fixtures run, which is the module's, not a
test's. A test that ran no measured line is absent from the recording.
"""

from __future__ import annotations

import json
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
# Rows in each table of the summary; the report holds them all.
SHOWN = 15


class Coverage(NamedTuple):
    """Each test's lines as one bit each, the one function of each test's lines the fewest tests run, and
    how many lines were measured."""

    lines: dict[str, int]
    reaches: dict[str, str]
    measured: int


def family(module: str) -> str:
    """The app or package a test module tests: what stands above its ``tests`` package, or its own package."""

    parts = module.split(".")
    if "tests" in parts:
        return ".".join(parts[: parts.index("tests")]) or "tests"
    return module.rpartition(".")[0] or module


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
            tests = [context for context in contexts if SEPARATOR in context]
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
    return Coverage(lines, reaches, measured)


def audit(found: Coverage) -> dict:
    lines = found.lines
    once = twice = 0
    for held in lines.values():
        twice |= once & held
        once |= held
    alone = once & ~twice
    by_family: dict[str, list[str]] = defaultdict(list)
    for test in lines:
        by_family[family(module_of(test))].append(test)
    candidates = []
    for tests in by_family.values():
        # The tests that run each one set of lines, and the sets by size, so only a larger set can hold one.
        sharing: dict[int, list[str]] = defaultdict(list)
        for test in tests:
            sharing[lines[test]].append(test)
        held = sorted((mine.bit_count(), mine) for mine in sharing)
        for n, (size, mine) in enumerate(held):
            wider = [
                test
                for other_size, other in held[n + 1 :]
                if other_size > size and other & mine == mine
                for test in sharing[other]
            ]
            for test in sharing[mine]:
                equal = [other for other in sharing[mine] if other != test]
                if equal or wider:
                    candidates.append((test, size, equal, wider))
    candidates.sort(key=lambda each: (-(len(each[2]) + len(each[3])), each[1], each[0]))
    rows = [
        {
            "test": label_of(test),
            "family": family(module_of(test)),
            "lines": size,
            "reaches": found.reaches.get(test, ""),
            "equal": sorted(map(label_of, equal)),
            # The nearest first: the wider test that runs the fewest lines besides.
            "wider": [label_of(other) for other in sorted(wider, key=lambda other: (lines[other].bit_count(), other))],
        }
        for test, size, equal, wider in candidates
    ]
    shared = sorted(test for test, held in lines.items() if not held & alone)
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[row["reaches"]].append(row["test"])
    named = Counter(row["family"] for row in rows)
    sharing_by_family = Counter(family(module_of(test)) for test in shared)
    return {
        "caveat": CAVEAT,
        "counts": {
            "tests": len(lines),
            "lines run": found.measured,
            "candidates": len(rows),
            "candidates equal to another": sum(1 for row in rows if row["equal"]),
            "without a line of their own": len(shared),
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
        "reaches": sorted(
            ({"reaches": reach, "candidates": tests} for reach, tests in grouped.items()),
            key=lambda row: (-len(row["candidates"]), row["reaches"]),
        ),
        "candidates": rows,
        "without a line of their own": [label_of(test) for test in shared],
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
