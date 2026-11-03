"""`mise run audit:mcp [--base <ref>] [--defer-build]`: what HQ's MCP tool list costs a client, and the server a process.

For the host alone and for the extension set dev:scratch composes, each measured by
tools.perf.mcp_probe in a process of its own, served as production serves it: the tools/list
answer's bytes and approximate tokens, each tool's part of it, the bytes of the prompt, resource and
resource template lists, of a describe_resources answer and of the action queue's page through
list_resource on the standard data set, and what loading the server and
answering its first requests added to the process, the API document's build and the SDK's modules
named apart, and what as many slow calls at once as the agent limits let in cost against the same
calls one after another. With ``--base``, the same of the ref beside this tree's, measured in a throwaway
worktree of it by this tree's probe. With ``--defer-build``, this tree again with the SDK's wire
models built on first use, as pydantic's ``defer_build`` would have them. Each run is kept as
var/perf/mcp-<commit>-<time>.json.

Tokens are approximate, four bytes of JSON to a token: the stack has no tokenizer, and a model's
own count is a request to its provider.
"""

import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hq.platform.core.tests import task
from tools.perf import runs
from tools.perf.mcp_probe import (
    ANSWERED,
    AT_ONCE,
    CATALOG,
    DEFER_BUILD,
    LISTS,
    LOADED,
    QUEUE,
    QUEUE_ITEMS,
    SERIAL,
    is_sdk,
)
from tools.perf.memory import group
from tools.perf.report import commit_of
from tools.perf.table import aligned

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var" / "perf"
# The probe and the ledger it counts with, laid over a base's tree so both sides count alike.
PROBE = ("tools/perf/mcp_probe.py", "tools/perf/memory_probe.py")
BYTES_PER_TOKEN = 4
SETS = ("host alone", "composed")
PARTS = ("description", "inputSchema", "outputSchema", "annotations")
TOP = 12
# Each side a run can measure, in the order its columns stand.
SIDES = ("base", "here", "deferred")
# The stages an agent's first request costs: the server's load and its first answers.
FIRST_REQUEST = (LOADED, ANSWERED)


def tokens(size: int) -> int:
    return round(size / BYTES_PER_TOKEN)


def by_package(modules: dict[str, dict[str, Any]], *, by: Callable[[str], str] = group) -> dict[str, int]:
    """Each package's own share of what the server's load and first answers added."""

    found: dict[str, int] = defaultdict(int)
    for name, entry in modules.items():
        if entry["stage"] in FIRST_REQUEST:
            found[by(name)] += entry["own"]
    return dict(found)


def sdk_share(modules: dict[str, dict[str, Any]]) -> int:
    """What the SDK's own modules took, of what the server's load and first answers added."""

    return sum(size for name, size in by_package(modules, by=str).items() if is_sdk(name))


def probe(tree: Path, env: dict[str, str]) -> dict[str, Any]:
    """The probe's result for ``tree``, on a throwaway database removed with its folder, holding
    the standard data set so a list read has items to cost."""

    with tempfile.TemporaryDirectory(prefix="audit-mcp-") as scratch:
        out = Path(scratch) / "measured.json"
        env = env | {"SEVERINO_DATABASE_PATH": str(Path(scratch) / "db.sqlite3")}
        for argv in (
            ["manage.py", "seed", "standard", "--owner", "audit-mcp"],
            ["-m", "tools.perf.mcp_probe", str(out)],
        ):
            done = subprocess.run(
                [sys.executable, *argv], cwd=tree, env=env, capture_output=True, text=True, check=False
            )
            if done.returncode:
                said = (done.stderr or done.stdout).strip().splitlines()[-30:]
                raise SystemExit(f"audit:mcp: {' '.join(argv)} in {tree} exited {done.returncode}:\n" + "\n".join(said))
        return json.loads(out.read_text(encoding="utf-8"))


def environments() -> dict[str, dict[str, str]]:
    """Each set's environment: no extension, and the ones mise.local.toml composes."""

    shared = os.environ | {
        "DJANGO_SETTINGS_MODULE": "hq.config.settings",
        "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
    }
    return {
        "host alone": shared | {"SEVERINO_HQ_PLUGINS": "", "PYTHONPATH": ""},
        "composed": shared
        | {
            "SEVERINO_HQ_PLUGINS": os.environ.get("HQ_LOCAL_PLUGINS", ""),
            "PYTHONPATH": os.environ.get("HQ_LOCAL_PYTHONPATH", ""),
        },
    }


def summary(result: dict[str, Any]) -> dict[str, int]:
    size = result["list"]["bytes"]
    return {
        "tools": result["list"]["tools"],
        "bytes": size,
        "tokens": tokens(size),
        **{method: result["lists"][method] for method in (*LISTS, CATALOG, QUEUE, QUEUE_ITEMS)},
        "queue per item": result["lists"][QUEUE] // max(result["lists"][QUEUE_ITEMS], 1),
        "server traced": result["traced"][LOADED],
        "server resident": result["resident"][LOADED],
        "document traced": result["document"]["traced"],
        "document peak": result["document"]["peak"],
        "document resident": result["document"]["resident"],
        "answer traced": result["traced"][ANSWERED],
        "answer resident": result["resident"][ANSWERED],
        "first request resident": result["resident"][LOADED] + result["resident"][ANSWERED],
        "sdk traced": sdk_share(result["modules"]),
        **{f"{phase} {key}": result["calls"][phase][key] for phase in (SERIAL, AT_ONCE) for key, _, _ in CALLS},
    }


def _mb(size: int) -> str:
    return f"{size / 1e6:.2f}"


# What each way of making the slow calls is counted by, and how its rows read.
CALLS = (
    ("resident", "resident MB while in flight", _mb),
    ("traced", "traced peak MB", _mb),
    ("threads", "threads at the peak", str),
    ("in flight", "calls in flight at the peak", str),
)

ROWS = (
    ("tools", "tools listed", str),
    ("bytes", "tools/list result bytes", str),
    ("tokens", "about tokens", str),
    *((method, f"{method} result bytes", str) for method in LISTS),
    (CATALOG, f"{CATALOG} answer bytes, no arguments", str),
    (QUEUE, f"{QUEUE} answer bytes, limit 100, standard set", str),
    (QUEUE_ITEMS, "  its items", str),
    ("queue per item", "  its bytes per item", str),
    ("server traced", "server load, traced MB", _mb),
    ("server resident", "server load, resident MB", _mb),
    ("document traced", "  the API document, traced MB", _mb),
    ("document peak", "  the API document, traced peak MB", _mb),
    ("document resident", "  the API document, resident MB", _mb),
    ("answer traced", "first initialize and tools/list, traced MB", _mb),
    ("answer resident", "first initialize and tools/list, resident MB", _mb),
    ("first request resident", "the two together, resident MB", _mb),
    ("sdk traced", "of both, the SDK's modules, traced MB", _mb),
    *(
        (f"{phase} {key}", f"slow calls {phase}, {title}", shown)
        for phase in (SERIAL, AT_ONCE)
        for key, title, shown in CALLS
    ),
)


def _indented(rows: list[list[str]], left: frozenset[int] = frozenset({0})) -> list[str]:
    return ["  " + row for row in aligned(rows, left)]


def _ranked(found: dict[str, int]) -> list[list[str]]:
    top = sorted(found.items(), key=lambda item: -item[1])[:TOP]
    return [["MB", "name"], *([_mb(size), name] for name, size in top)]


def render(kept: dict[str, Any]) -> str:
    sides = [side for side in SIDES if side in kept]
    columns = [(name, side) for name in SETS for side in sides]
    label = {side: kept[side]["commit"] + (", deferred" if side == "deferred" else "") for side in sides}
    table = [["", *(f"{name}, {label[side]}" for name, side in columns)]]
    for key, title, shown in ROWS:
        table.append([title, *(shown(kept[side]["sets"][name]["summary"][key]) for name, side in columns)])
    ready = sorted({kept[side]["sets"][name]["result"]["ready"] for name, side in columns})
    lines = [
        f"audit:mcp {kept['here']['commit']}"
        + (f" against {kept['base']['commit']}" if "base" in kept else "")
        + f", composed: {', '.join(kept['extensions']) or 'none'}",
        (
            "Counted in a served process (imports lazy, the readiness page answered "
            f"{', '.join(map(str, ready))}): tools/list's result as the server's HTTP transport answers it, "
            f"at {BYTES_PER_TOKEN} bytes a token; each stage as tracemalloc traces it, and as the peak "
            "resident set grows over it, less tracemalloc's own. The slow calls, each one agent's, as many "
            "as the agent limits let every agent make at once, come last: resident as ps reads it while "
            "they are in flight."
        ),
        "",
        *aligned(table),
    ]
    for side in ("base", "here"):
        if side not in kept:
            continue
        measured = kept[side]["sets"]["composed"]["result"]
        rows = [["tool", *PARTS, "bytes"]]
        for name, parts in sorted(measured["tools"].items(), key=lambda item: -sum(item[1].values())):
            rows.append([name, *(str(parts.get(part, 0)) for part in PARTS), str(sum(parts.values()))])
        lines += ["", f"Each tool's bytes, composed, {kept[side]['commit']}:", *_indented(rows)]
    modules = kept["here"]["sets"]["composed"]["result"]["modules"]
    sdk = {name: size for name, size in by_package(modules, by=str).items() if is_sdk(name)}
    lines += [
        "",
        "Each package's own share of the server's load and first answers, traced, composed:",
        *_indented(_ranked(by_package(modules)), frozenset({1})),
        "",
        "Each SDK module's own share of them, traced, composed:",
        *_indented(_ranked(sdk), frozenset({1})),
    ]
    return "\n".join(lines)


def measured(tree: Path, commit: str, extra: dict[str, str] | None = None) -> dict[str, Any]:
    # Compiled first, so neither side's load is charged for compiling its source.
    subprocess.run([sys.executable, "-m", "compileall", "-q", "hq", "tools"], cwd=tree, check=True)
    sets = {}
    for name, env in environments().items():
        result = probe(tree, env | (extra or {}))
        sets[name] = {"summary": summary(result), "result": result}
    return {"commit": commit, "sets": sets}


def main() -> int:
    usage = task.usage()
    base = usage.text("base")
    here = commit_of(ROOT)
    fields: dict[str, Any] = {
        "extensions": [spec for spec in os.environ.get("HQ_LOCAL_PLUGINS", "").split(",") if spec],
        "here": measured(ROOT, here),
    }
    if usage.switch("defer_build"):
        fields["deferred"] = measured(ROOT, here, {DEFER_BUILD: "1"})
    if base:
        try:
            commit = task.commit_of(base)
        except task.RefError as error:
            print(f"audit:mcp: {error}", file=sys.stderr)
            return 2
        with task.worktree(commit) as tree:
            task.overlay(tree, PROBE, ())
            fields["base"] = measured(tree, commit[:12])
    kept = runs.Run.start(OUT, f"mcp-{fields['here']['commit']}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}", **fields)
    print(render(kept.doc), "", f"Kept: {kept.path.relative_to(ROOT)}", sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
