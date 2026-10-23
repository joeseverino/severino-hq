"""The structural bar from AGENTS.md, as a gate: index this checkout in a code
knowledge graph and fail on a near-duplicate function pair outside tests that
the baseline does not list. Tests answer "does this behave?"; this answers "is
it still one system?". A file's size is a test's (FileSizeTests).

    mise run checks:structural             check
    mise run checks:structural --record    rewrite the baseline from this tree

The graph is codebase-memory-mcp at the release mise.toml pins, installed and
found through mise, since the answer's shape is that release's. It is lazy
there, so this installs it first. Exit 3 when mise cannot, which fails the gate.
An answer the classifier refuses fails it too, among them one with no pairs at
all: the tests alone hold some, so that graph indexed nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.structural.classify import similar_pairs

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("baseline.txt")
TOOL = "codebase-memory-mcp"
PACKAGE = f"github:DeusData/{TOOL}"
QUERY = "MATCH (a)-[r:SIMILAR_TO]->(b) RETURN a.file_path, b.file_path, a.name, b.name"
HEADER = (
    "# Structural baseline for `mise run checks:structural`: each near-duplicate\n"
    "# pair outside tests. Pairs only leave. Rewrite with --record.\n"
)


def graph_binary() -> str:
    for command in (["mise", "install", PACKAGE], ["mise", "which", TOOL]):
        found = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        if found.returncode != 0:
            print(f"mise has no {TOOL}; the structural bar did not run.\n{found.stderr}", file=sys.stderr)
            raise SystemExit(3)
    return found.stdout.strip()


def cli(binary: str, store: Path, tool: str, arguments: dict) -> dict:
    # The graph's store and daemon directory are the gate's own, so it meets no
    # daemon an editor started from another build and leaves nothing behind.
    env = os.environ | {"CBM_CACHE_DIR": str(store / "cache"), "CBM_RUNTIME_DIR": str(store / "run")}
    # The tool says why it could not answer; without that this fails silently.
    answer = subprocess.run(
        [binary, "cli", "--json", tool],
        input=json.dumps(arguments),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if answer.returncode != 0:
        print(answer.stderr, file=sys.stderr)
        raise SystemExit(1)
    return json.loads(answer.stdout)


def graph_pages(binary: str, store: Path, project: str) -> list[dict]:
    """Every page of the answer: the graph pages a large result, and the
    classifier refuses one that does not add up to its total."""

    pages, cursor = [], None
    while True:
        page = cli(
            binary,
            store,
            "query_graph",
            {"project": project, "query": QUERY, "format": "json", "max_rows": 99998}
            | ({"cursor": cursor} if cursor else {}),
        )
        pages.append(page)
        answer = json.loads(page["content"][0]["text"])
        cursor = answer.get("next_cursor") if answer.get("has_more") else None
        if not cursor:
            return pages


def build_inspector(directory: Path) -> Path:
    """The Go syntax reader, from the controller's module: it uses the standard
    library only, so no provider code is loaded."""

    program = directory / "structural-inspector"
    subprocess.run(
        ["go", "build", "-o", str(program), "./cmd/structural-inspector"], cwd=ROOT / "controller", check=True
    )
    return program


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="checks:structural")
    parser.add_argument("--record", action="store_true", help="rewrite the baseline from this tree")
    args = parser.parse_args(argv)

    binary = graph_binary()
    project = "structural-bar"
    # Under /tmp: the daemon's socket lives in the store, and a socket's path
    # must fit 104 bytes, which macOS's per-user temporary directory overruns.
    with tempfile.TemporaryDirectory(prefix="cbm-", dir="/tmp") as scratch:
        store = Path(scratch)
        for part in ("cache", "run"):
            (store / part).mkdir(mode=0o700)
        cli(binary, store, "index_repository", {"repo_path": str(ROOT), "mode": "full", "name": project})
        try:
            current = similar_pairs(ROOT, build_inspector(store), graph_pages(binary, store, project))
        except ValueError as refused:
            print(f"the structural bar cannot trust the graph: {refused}", file=sys.stderr)
            return 1

    if args.record:
        BASELINE.write_text(HEADER + "".join(f"{line}\n" for line in current))
        print("structural baseline recorded")
        return 0
    return compare(current)


def compare(current: list[str]) -> int:
    """Fail on each pair the baseline does not list; a listed pair that left passes."""

    known = set(BASELINE.read_text().splitlines())
    new = [line for line in current if line not in known]
    for line in new:
        print(f"new near-duplicate outside tests: {line.removeprefix('similar ')}", file=sys.stderr)
    if new:
        return 1
    print("structural bar holds")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
