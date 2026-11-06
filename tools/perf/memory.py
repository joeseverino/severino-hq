"""`mise run audit:memory [--seed <name>] [--host-alone] [--top <n>] [--after <paths>]`: what each module costs in memory.

Each module's and package's own share of HQ's Python memory, at boot and after a pass over the
navigation's pages, ranked, as tools.perf.memory_probe counts it in a process of its own. The
database is a throwaway one, seeded as dev:scratch seeds it, and the extensions are the ones
dev:scratch composes. The probe serves as the image does, with DEBUG off. With ``--after``, each
path boot never takes (memory_probe.AFTER) is a stage of its own after the pass, its modules'
import times read from ``python -X importtime`` in a second, untraced run. Each run is kept as
var/perf/memory-<commit>-<time>.json, the share of every module and every package's row in it.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

lazy import django
lazy from django.core.management import call_command
lazy from django.test import override_settings

from hq.platform.core.attribution import from_the_shell
from hq.platform.core.tests.task import usage
from tools.perf import parse, runs
from tools.perf.memory_probe import AFTER, BOOT, MARK, PASS, READY
from tools.perf.report import commit_of
from tools.perf.table import aligned
lazy from hq.platform.core import seeds
lazy from hq.platform.core.tests.browser.estate import navigation

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var" / "perf"
USER = "operator"
MEASURED = "Python's allocator, as tracemalloc counts it: no C library's own allocations"
# A package shown at this many parts of its name, so HQ's own layers and Django's parts stand
# apart; any other by its top-level name.
DEPTH = {"hq": 3, "django": 2}
STAGES = {BOOT: "at boot", PASS: "added by the pass"}
# A line python -X importtime writes: a module's own microseconds, then with its imports, then its
# name, indented by its depth.
_IMPORT_TIME = re.compile(r"^import time:\s+(\d+) \|\s+(\d+) \| +(\S+)$")


def group(module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[: DEPTH.get(parts[0], 1)])


def ranked(modules: dict[str, dict[str, Any]], stage: str, *, by=group) -> list[tuple[str, int, int]]:
    """(name, own bytes, modules) for each name ``by`` gives the modules loaded in ``stage``, the
    largest first."""

    size: dict[str, int] = defaultdict(int)
    count: Counter[str] = Counter()
    for module, entry in modules.items():
        if entry["stage"] == stage:
            size[by(module)] += entry["own"]
            count[by(module)] += 1
    return sorted(((name, size[name], count[name]) for name in size), key=lambda row: (-row[1], row[0]))


def stages(result: dict[str, Any]) -> dict[str, str]:
    """Each stage the run measured, in order, by how it is said."""

    return STAGES | {path: f"added by {path}" for path in result.get("after", {})}


def import_times(said: str) -> dict[str, dict[str, Any]]:
    """Each module python -X importtime timed after the pass, by the path whose load it was in: its
    own microseconds and those with the imports it started."""

    timed: dict[str, dict[str, Any]] = {}
    path = ""
    for line in said.splitlines():
        if line.startswith(MARK):
            path = line.removeprefix(MARK).strip()
        elif path and (found := _IMPORT_TIME.match(line)):
            timed[found[3]] = {"self": int(found[1]), "cumulative": int(found[2]), "stage": path}
    return timed


def _mb(size: int) -> str:
    return f"{size / 1e6:.2f}"


def _table(title: str, rows: Iterable[tuple[str, int, int]], top: int) -> list[str]:
    shown = [["MB", "modules", "name"], *([_mb(size), str(count), name] for name, size, count in list(rows)[:top])]
    return [title, *("  " + line for line in aligned(shown))]


def totals(result: dict[str, Any]) -> dict[str, dict[str, int]]:
    """For each stage: what tracemalloc held at its end, or added over it; the modules it loaded;
    their own shares summed; and what was allocated outside any load."""

    found = {}
    held = 0
    for stage in stages(result):
        entries = [entry["own"] for entry in result["modules"].values() if entry["stage"] == stage]
        reached = result["after"][stage] if stage in result.get("after", {}) else result[stage]
        traced = reached - held
        held = reached
        found[stage] = {
            "traced": traced,
            "modules": len(entries),
            "own": sum(entries),
            "outside": traced - sum(entries),
        }
    return found


def answered(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each page the probe asked, the readiness probe first, by how it answered."""

    return {READY: result["ready"], **result["pages"]}


def render(kept: dict[str, Any], top: int) -> str:
    result, counted = kept["result"], kept["totals"]
    composed = ", ".join(kept["extensions"]) or "none, the host alone"
    refused = [
        f"{page} {landed['status']}" + (f" to {landed['to']}" if landed["to"] else "")
        for page, landed in answered(result).items()
        if landed["status"] != 200
    ]
    lines = [
        f"audit:memory {kept['commit']}, seeded {kept['seed']}, extensions: {composed}",
        f"Counted: {MEASURED}.",
        f"Boot ends on {READY}; pages walked after it: {len(result['pages'])}; "
        + (f"not 200: {', '.join(refused)}" if refused else "every one 200"),
        f"Peak: {_mb(result['peak'])} MB",
        "",
    ]
    timed = kept.get("import_times", {})
    for stage, said in stages(result).items():
        total = counted[stage]
        took = sum(entry["self"] for entry in timed.values() if entry["stage"] == stage)
        lines.append(
            f"{said.capitalize()}: {_mb(total['traced'])} MB over {total['modules']} modules loaded, "
            f"{_mb(total['own'])} MB their own, {_mb(total['outside'])} MB allocated outside any load"
            + (f"; {took / 1000:.1f} ms importing, untraced" if stage in result.get("after", {}) else "")
        )
    lines.append(f"After the pass: {_mb(result[PASS])} MB")
    for path, reached in result.get("after", {}).items():
        lines.append(f"After {path}: {_mb(reached)} MB")
    for stage, said in stages(result).items():
        lines += ["", *_table(f"By package, {said}:", ranked(result["modules"], stage), top)]
        lines += ["", *_table(f"By module, {said}:", ranked(result["modules"], stage, by=str), top)]
        if stage in result.get("after", {}):
            slowest = sorted(
                ((name, entry) for name, entry in timed.items() if entry["stage"] == stage),
                key=lambda row: (-row[1]["self"], row[0]),
            )
            shown = [["ms", "with imports", "module"]]
            shown += [[f"{e['self'] / 1000:.1f}", f"{e['cumulative'] / 1000:.1f}", name] for name, e in slowest[:top]]
            lines += ["", f"Slowest imports, {said}:", *("  " + line for line in aligned(shown))]
    return "\n".join(lines)


def prepare(out: str) -> None:
    """In the composed environment: the database migrated and seeded, a session for its owner, and
    the pages its navigation lists under the settings the set is shown with."""

    django.setup()
    seed = os.environ["MEMORY_SEED"]
    said = io.StringIO()
    # The commands run in this process rather than through manage.py, so their door binds here.
    with from_the_shell("perf.memory.prepare"):
        call_command("seed", seed, owner=USER, verbosity=0)
        call_command("dev_session", USER, stdout=said)
    with override_settings(**seeds.get(seed).settings):
        pages = navigation()
    Path(out).write_text(
        json.dumps({"session": parse.cookie(said.getvalue())[1], "pages": pages}),
        encoding="utf-8",
    )


def _child(argv: list[str], env: dict[str, str]) -> str:
    """What the child said on stderr, once it exited 0."""

    done = subprocess.run([sys.executable, *argv], cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    if done.returncode:
        said = (done.stderr or done.stdout).strip().splitlines()[-30:]
        raise SystemExit(f"audit:memory: {' '.join(argv)} exited {done.returncode}:\n" + "\n".join(said))
    return done.stderr


def measure(env: dict[str, str], seed: str, paths: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The probe's result and the import times of ``paths``, measured in a scratch folder removed
    however it ends, its database and every file it writes in it.

    The probe serves as the image does: DEBUG off, its assets collected, so the readiness probe
    that ends its boot reads their manifest as production's does."""

    with tempfile.TemporaryDirectory(prefix="audit-memory-") as scratch:
        work = Path(scratch)
        places = {name: work / name for name in ("static", "media", "exports")}
        for place in places.values():
            place.mkdir()
        env = env | {
            "SEVERINO_DATABASE_PATH": str(work / "db.sqlite3"),
            "DJANGO_STATIC_ROOT": str(places["static"]),
            "SEVERINO_MEDIA_ROOT": str(places["media"]),
            "SEVERINO_EXPORTS_ROOT": str(places["exports"]),
        }
        _child(["-m", "tools.perf.memory", "prepare", str(work / "prepared.json")], env)
        plan = json.loads((work / "prepared.json").read_text(encoding="utf-8"))
        served = env | dict(seeds.get(seed).settings) | {"DJANGO_DEBUG": "0"}
        _child(["manage.py", "collectstatic", "--noinput", "--verbosity", "0"], served)
        asked = {
            "MEMORY_PAGES": " ".join(plan["pages"]),
            "MEMORY_SESSION": plan["session"],
            "MEMORY_AFTER": " ".join(paths),
        }
        _child(["-m", "tools.perf.memory_probe", str(work / "measured.json")], served | asked)
        timed = {}
        if paths:
            said = _child(["-X", "importtime", "-m", "tools.perf.memory_probe"], served | asked | {"MEMORY_TIMED": "1"})
            timed = import_times(said)
        return json.loads((work / "measured.json").read_text(encoding="utf-8")), timed


def main() -> int:
    """0 when every page answered 200; 1 when one did not; 2 for a set no server is seeded with."""

    asked = usage()
    seed, top = asked.text("seed"), asked.text("top")
    if seed not in seeds.SEEDS:
        print(f"audit:memory: no data set {seed!r}. Sets: {', '.join(seeds.SEEDS)}", file=sys.stderr)
        return 2
    if not top.isdigit():
        print(f"audit:memory: --top {top!r} is not a count of rows", file=sys.stderr)
        return 2
    paths = [path for path in asked.text("after").split(",") if path]
    if unknown := [path for path in paths if path not in AFTER]:
        print(f"audit:memory: no path {', '.join(unknown)}. Paths: {', '.join(AFTER)}", file=sys.stderr)
        return 2
    alone = asked.switch("host_alone")
    plugins = "" if alone else os.environ.get("HQ_LOCAL_PLUGINS", "")
    env = os.environ | {
        "DJANGO_SETTINGS_MODULE": "hq.config.settings",
        "SEVERINO_HQ_PLUGINS": plugins,
        "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
        "PYTHONPATH": "" if alone else os.environ.get("HQ_LOCAL_PYTHONPATH", ""),
        "MEMORY_SEED": seed,
    }
    started = time.monotonic()
    result, timed = measure(env, seed, paths)
    commit = commit_of(ROOT)
    kept = runs.Run.start(
        OUT,
        f"memory-{commit}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}",
        commit=commit,
        seed=seed,
        extensions=[spec for spec in plugins.split(",") if spec],
        measured=MEASURED,
        seconds=round(time.monotonic() - started, 1),
        totals=totals(result),
        packages={stage: ranked(result["modules"], stage) for stage in stages(result)},
        import_times=timed,
        result=result,
    )
    print(
        render(kept.doc, int(top)),
        "",
        f"Every row the tables leave out, each module's and each package's by stage, is in "
        f"{kept.path.relative_to(ROOT)} (.result.modules, .packages, .import_times)",
        sep="\n",
    )
    return 0 if all(landed["status"] == 200 for landed in answered(result).values()) else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["prepare"]:
        prepare(sys.argv[2])
    else:
        sys.exit(main())
