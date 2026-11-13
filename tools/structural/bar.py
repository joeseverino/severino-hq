"""The structural bar as a gate: fail on a near-duplicate function pair
outside tests that the baseline does not list, so the code stays one system
(Structural Gate, in docs). ``--record`` rewrites the baseline from this tree;
``--pairs`` first prints every pair outside tests with what explains it, so a
change to the classifier is read as a diff of that list.

The graph is the codebase-memory-mcp release mise.toml pins, since the
answer's shape is that release's; an answer the classifier refuses fails too.
The pairs are kept under var/structural by a digest of everything they depend
on (``inputs_key``), so a run on inputs already read answers without the graph.
The graph's own store stays there too, so the graph reindexes only what changed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hq.platform.application.ui import counted
from hq.platform.core.tests.task import Usage, usage
from tools.gate.tree import working_tree
from tools.structural.classify import similar_pairs

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("baseline.txt")
TOOL = "codebase-memory-mcp"
# Each function's MinHash fingerprint, compared here at the classifier's
# threshold: the graph's own SIMILAR_TO edges stop at a fixed, higher one.
QUERY = "MATCH (f) WHERE f:Function OR f:Method RETURN f.file_path, f.name, f.parent_class, f.fp"
# The answer for each set of inputs the bar has read, by its key: an
# unchanged tree answers without indexing it again. The newest KEPT stay.
CACHE = ROOT / "var" / "structural"
KEPT = 16
HEADER = (
    "# Structural baseline for `mise run lint:structural`: each near-duplicate\n"
    "# pair outside tests. Pairs only leave. Rewrite with --record.\n"
)


def cli(store: Path, tool: str, arguments: dict) -> dict:
    # The graph's store and daemon directory are the gate's own, so it meets no
    # daemon an editor started from another build and no index of another tree.
    env = os.environ | {"CBM_CACHE_DIR": str(CACHE / "graph"), "CBM_RUNTIME_DIR": str(store / "run")}
    # The tool says why it could not answer; without that this fails silently.
    answer = subprocess.run(
        [TOOL, "cli", "--json", tool],
        input=json.dumps(arguments),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if answer.returncode != 0:
        # A tool killed by a signal (the memory limit's, say) writes no stderr.
        ended = f"signal {-answer.returncode}" if answer.returncode < 0 else f"status {answer.returncode}"
        print(f"structural bar: the graph's {tool} ended on {ended}", answer.stderr, sep="\n", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(answer.stdout)


def graph_pages(store: Path, project: str) -> list[dict]:
    """Every page of the answer: the graph pages a large result, and the
    classifier refuses one that does not add up to its total."""

    pages, cursor = [], None
    while True:
        page = cli(
            store,
            "query_graph",
            # The whole answer in one page where it fits: each page re-runs the query.
            {"project": project, "query": QUERY, "format": "json", "max_rows": 99998, "max_output_tokens": 1000000}
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


def main(asked: Usage) -> int:
    key = inputs_key()
    current = cached(key)
    if current is None:
        current = measure()
        if current is None:
            return 1
        keep(key, current)
    else:
        print(f"structural bar: answered from {CACHE.relative_to(ROOT)}, which read these same inputs", file=sys.stderr)

    # One line, so a gate's verdict is never pushed out of view by pairs that pass.
    explained = sum(1 for reason in current.values() if reason)
    print(f"{counted(explained, 'similar pair', 'similar pairs')} explained by a classification", file=sys.stderr)
    if asked.switch("pairs"):
        for line, reason in current.items():
            print(f"{line.removeprefix('similar ')}\t{reason or 'unexplained'}")
    unexplained = [line for line, reason in current.items() if not reason]
    if asked.switch("record"):
        BASELINE.write_text(HEADER + "".join(f"{line}\n" for line in unexplained))
        print("structural baseline recorded")
        return 0
    return compare(unexplained)


def measure() -> dict[str, str | None] | None:
    """Every pair outside tests with what explains it, from a graph of this
    tree; None when the classifier refuses the graph."""

    project = "structural-bar"
    # The graph's indexes outlive the run, so the graph's own incremental
    # route reindexes only the files whose size, mtime or content moved, and
    # rebuilds whole on a store it cannot trust or a new index format. A lock
    # keeps a second run in this checkout from writing the store at once.
    indexes = CACHE / "graph"
    indexes.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Under /tmp: the daemon's socket lives in its run directory, and a socket's
    # path must fit 104 bytes, which macOS's per-user temporary directory overruns.
    with (
        (indexes / ".lock").open("w") as held,
        tempfile.TemporaryDirectory(prefix="cbm-", dir="/tmp") as scratch,
    ):
        fcntl.flock(held, fcntl.LOCK_EX)
        store = Path(scratch)
        (store / "run").mkdir(mode=0o700)
        cli(store, "index_repository", {"repo_path": str(ROOT), "mode": "full", "name": project})
        try:
            return similar_pairs(ROOT, build_inspector(store), graph_pages(store, project))
        except ValueError as refused:
            print(f"the structural bar cannot trust the graph: {refused}", file=sys.stderr)
            return None


def inputs_key() -> str:
    """Everything the pairs depend on, as one digest: the graph and Go
    releases that read the tree, which files are tracked (the classifier
    trusts only tracked generator provenance), and the content of every file
    git sees, the bar's own among them, since the graph indexes those and none
    git ignores. The baseline is left out: it is compared after."""

    digest = hashlib.sha256()
    for command in ([TOOL, "--version"], ["go", "version"], ["git", "ls-files", "-z"]):
        digest.update(subprocess.run(command, cwd=ROOT, check=True, capture_output=True).stdout)
    # Only the repository's ignore rules apply: the graph never reads the user's global ones.
    digest.update(working_tree(ROOT, user_ignores=False, leaving=[str(BASELINE.relative_to(ROOT))]).encode())
    return digest.hexdigest()


def cached(key: str) -> dict[str, str | None] | None:
    """The pairs a run with these inputs found, or None when none is kept."""

    entry = CACHE / f"{key}.json"
    try:
        pairs = json.loads(entry.read_text())
    except OSError, ValueError:
        return None
    # The newest use keeps an entry, so the KEPT most recently used stay.
    entry.touch()
    return pairs


def keep(key: str, pairs: dict[str, str | None]) -> None:
    """Keep these pairs as the answer for these inputs, and drop all but the KEPT newest entries."""

    CACHE.mkdir(parents=True, exist_ok=True)
    written = CACHE / f".{key}.{os.getpid()}"
    written.write_text(json.dumps(pairs))
    # A rename, so a run beside this one reads a whole entry or none.
    written.replace(CACHE / f"{key}.json")
    for stale in sorted(CACHE.glob("*.json"), key=last_used, reverse=True)[KEPT:]:
        stale.unlink(missing_ok=True)


def last_used(entry: Path) -> float:
    """When an entry was last written or read; a run beside this one may have just dropped it."""

    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def compare(current: list[str]) -> int:
    """Fail on each pair the baseline does not list; a listed pair that left passes."""

    known = set(BASELINE.read_text().splitlines())
    new = [line for line in current if line not in known]
    for line in new:
        print(f"new near-duplicate outside tests: {line.removeprefix('similar ')}", file=sys.stderr)
    if new:
        print(
            f"{counted(len(new), 'new near-duplicate pair', 'new near-duplicate pairs')} outside tests. The baseline only"
            " shrinks, so the cure is to merge each pair into one function. `mise run lint:structural` lists them.",
            file=sys.stderr,
        )
        return 1
    print("structural bar holds")
    return 0


if __name__ == "__main__":
    sys.exit(main(usage()))
