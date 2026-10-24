"""`mise run perf:ab <base> [page]...`: which change moved a page's server time, and by how much.

perf:report answers how light and fast HQ is, server and browser, beside a
base; this answers whether a difference in server time is the code's. Each
side is its tree's own Django rendering through its test client (the text of
tools/perf/ab_probe.py, run in that tree): this checkout as it stands, or with
`--here <ref>` a worktree of that ref, against a worktree of `<base>`, each on
a new database seeded with the example estate. No server and no browser, so a
round is cheap: the two sides are asked page by page, alternating which goes
first each round, over `--rounds` rounds, so a machine busy for a moment
weighs on both alike. A page is the pages both trees' navigation lists unless
named.

Each page's row gives the records its tables hold and its HTML's size on this
side beside how far each moved from the base's (a page drawing more rows is a
different page, not slower code), its queries likewise, the median server
time on each side, and the median of the round-by-round difference with its
interquartile range: a difference inside its spread is noise. The time is the
Server-Timing header's `app` metric, the server's own, as perf:report reads it.
A tree that refuses the extension set for its plugin API makes both sides run
host alone, as `--host-alone` asks, and the closing line says so.
"""

import json
import os
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from tools.gate.at import RefError, git, remove
from tools.perf import parse, table
from tools.perf.targets import USER, NotMeasured, clean_env, showcase, stop_group

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var" / "perf"
PROBE = Path(__file__).with_name("ab_probe.py")
READY = "perf-ab-ready "
MARKER = "perf-ab "
# Rounds asked of each page before the timed ones, so what a first request caches is not its cost.
WARM = 2


def usage(name: str) -> str:
    return os.environ.get(f"usage_{name}", "")


@dataclass
class Side:
    """One tree's Django, answering page by page."""

    name: str
    commit: str
    process: subprocess.Popen
    log: Path
    pages: list[str] = field(default_factory=list)

    def _line(self) -> str:
        assert self.process.stdout is not None
        while line := self.process.stdout.readline():
            if line.startswith((READY, MARKER)):
                return line
        tail = "\n".join(self.log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
        raise NotMeasured(f"{self.name} ({self.commit}) exited before it answered:\n{tail}")

    def ready(self) -> None:
        self.pages = json.loads(self._line().removeprefix(READY))

    def ask(self, page: str, **flags: bool) -> dict[str, Any]:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"page": page, **flags}) + "\n")
        self.process.stdin.flush()
        return json.loads(self._line().removeprefix(MARKER))

    def stop(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        stop_group(self.process)


def side_env(folder: Path, alone: bool) -> dict[str, str]:
    """What a side's Django runs under: the scratch server's development settings, a database of
    its own, and the extension set this task was given unless ``alone``."""

    return clean_env(
        DJANGO_SETTINGS_MODULE="hq.config.settings",
        DJANGO_DEBUG="1",
        DJANGO_SECRET_KEY="perf-ab-only-key-0123456789abcdef0123456789abcdef",
        DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost,testserver",
        SEVERINO_DATABASE_PATH=str(folder / "db.sqlite3"),
        SEVERINO_MEDIA_ROOT=str(folder / "media"),
        SEVERINO_EXPORTS_ROOT=str(folder / "exports"),
        SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION="0",
        SEVERINO_LOG_LEVEL="WARNING",
        **showcase(),
        SEVERINO_HQ_PLUGINS="" if alone else os.environ.get("SEVERINO_HQ_PLUGINS", ""),
        PYTHONPATH="" if alone else os.environ.get("PYTHONPATH", ""),
        PERF_USER=USER,
    )


@contextmanager
def tree_of(ref: str | None) -> Iterator[tuple[Path, str]]:
    """This checkout for no ref, else a detached worktree of ``ref`` outside it, gone however the block ends."""

    if ref is None:
        sha = git("rev-parse", "--short=12", "HEAD")[0]
        changed = bool(git("status", "--porcelain"))
        yield ROOT, f"{sha}-changed" if changed else sha
        return
    commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")[0]
    folder = Path(tempfile.mkdtemp(prefix="hq-perf-ab-"))
    tree = folder / "checkout"
    try:
        git("worktree", "add", "--detach", "--quiet", str(tree), commit)
        yield tree, commit[:12]
    finally:
        remove(tree)
        shutil.rmtree(folder, ignore_errors=True)


def start(name: str, tree: Path, commit: str, alone: bool, stack: ExitStack) -> Side:
    folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=f"hq-perf-ab-{name}-")))
    log = OUT / f"ab-{commit}-{name}.log"
    sink: IO[str] = stack.enter_context(log.open("w", encoding="utf-8"))
    code = PROBE.read_text(encoding="utf-8")
    process = subprocess.Popen(
        ["uv", "run", "--quiet", "python", "manage.py", "shell", "--no-imports", "-c", code],
        cwd=tree,
        env=side_env(folder, alone),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sink,
        text=True,
        start_new_session=True,
    )
    side = Side(name, commit, process, log)
    stack.callback(side.stop)
    return side


def server_ms(said: dict[str, Any]) -> float:
    """The server's own time for one answer, or the client's where the tree sends no Server-Timing."""

    took = parse.server_time(said["timing"])
    return took if took is not None else said["wall_ms"]


def compare(base: Side, here: Side, pages: list[str], rounds: int) -> dict[str, dict[str, Any]]:
    """Each page asked of both sides, alternating which goes first each round."""

    found: dict[str, dict[str, Any]] = {}
    for page in pages:
        for _ in range(WARM):
            for side in (base, here):
                side.ask(page)
        # Read once warm, so what a first request caches is not counted as the page's.
        warm = {side.name: side.ask(page, html=True, queries=True) for side in (base, here)}
        found[page] = {
            side: {
                "status": said["status"],
                "rows": parse.data_rows(said["html"]),
                "html_bytes": said["bytes"],
                "queries": said["queries"],
                "ms": [],
            }
            for side, said in warm.items()
        }
    for turn in range(rounds):
        order = (base, here) if turn % 2 == 0 else (here, base)
        for page in pages:
            for side in order:
                found[page][side.name]["ms"].append(server_ms(side.ask(page)))
    return found


def summarize(found: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each page's numbers on both sides, and the median and spread of the round-by-round difference."""

    rows = []
    for page, sides in found.items():
        base, here = sides["base"], sides["here"]
        moved = [mine - theirs for mine, theirs in zip(here["ms"], base["ms"], strict=True)]
        quartiles = statistics.quantiles(moved, n=4) if len(moved) > 1 else [0.0, 0.0, 0.0]
        rows.append(
            {
                "page": page,
                "base": {key: value for key, value in base.items() if key != "ms"},
                "here": {key: value for key, value in here.items() if key != "ms"},
                "base_ms": statistics.median(base["ms"]),
                "here_ms": statistics.median(here["ms"]),
                "moved_ms": statistics.median(moved),
                "spread_ms": quartiles[2] - quartiles[0],
            }
        )
    return rows


HEADERS = ("page", "rows", "HTML KB", "queries", "base ms", "here ms", "moved ms", "spread ms", "")


def render(rows: list[dict[str, Any]]) -> list[str]:
    """The table: a page's own numbers beside how far each moved from the base's, its times on each
    side, and the difference, marked where it stands outside its spread."""

    lines = [list(HEADERS)]
    for row in rows:
        mine, theirs = row["here"], row["base"]
        page = row["page"] if mine["status"] == 200 else f"{row['page']} ({mine['status']})"
        outside = abs(row["moved_ms"]) > row["spread_ms"] / 2
        lines.append(
            [
                page,
                table.cell(mine["rows"], theirs["rows"], 0, True),
                table.cell(mine["html_bytes"] / 1024, theirs["html_bytes"] / 1024, 1, True),
                table.cell(mine["queries"], theirs["queries"], 0, True),
                f"{row['base_ms']:.2f}",
                f"{row['here_ms']:.2f}",
                f"{row['moved_ms']:+.2f}",
                f"{row['spread_ms']:.2f}",
                "moved" if outside else "",
            ]
        )
    return table.aligned(lines)


def measure(base_ref: str, here_ref: str | None, named: list[str], rounds: int, alone: bool) -> tuple[list, str, str]:
    """The pages' rows, and each side's commit."""

    with ExitStack() as stack:
        base_tree, base_commit = stack.enter_context(tree_of(base_ref))
        here_tree, here_commit = stack.enter_context(tree_of(here_ref))
        print(f"Starting {base_commit} and {here_commit}: migrating and seeding a database each", flush=True)
        base = start("base", base_tree, base_commit, alone, stack)
        here = start("here", here_tree, here_commit, alone, stack)
        for side in (base, here):
            side.ready()
        pages = named or [page for page in here.pages if page in base.pages]
        if not pages:
            raise NotMeasured("neither side lists its pages, so name them")
        print(f"{len(pages)} pages, {rounds} rounds, alternating", flush=True)
        return summarize(compare(base, here, pages, rounds)), base_commit, here_commit


def refusal(error: NotMeasured) -> str | None:
    return parse.api_refusal(str(error))


def stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def main() -> int:
    signal.signal(signal.SIGTERM, stop)
    OUT.mkdir(parents=True, exist_ok=True)
    base_ref, here_ref = usage("base"), usage("here") or None
    # mise exports a variadic argument shell-quoted.
    named = shlex.split(usage("page"))
    rounds = int(usage("rounds") or 30)
    alone = "--host-alone asked for it" if usage("host_alone") == "true" else ""
    try:
        try:
            rows, base_commit, here_commit = measure(base_ref, here_ref, named, rounds, bool(alone))
        except NotMeasured as error:
            if alone or not (refused := refusal(error)):
                raise
            alone = f"a side refused the extension set: {refused}"
            print(f"{alone}; running both sides host alone instead", flush=True)
            rows, base_commit, here_commit = measure(base_ref, here_ref, named, rounds, True)
    except RefError as error:
        print(f"perf:ab: {error}", file=sys.stderr)
        return 2
    except NotMeasured as error:
        print(f"perf:ab measured nothing: {error}", file=sys.stderr)
        return 1
    print(f"perf:ab {here_commit} beside {base_commit}: median server ms over {rounds} rounds, Django test client")
    print("\n".join(render(rows)))
    moved = [row["page"] for row in rows if abs(row["moved_ms"]) > row["spread_ms"] / 2]
    said = f"{len(moved)} of {len(rows)} pages moved past their spread"
    if alone:
        said += f"; the host measured alone on both sides because {alone}"
    print(f"\nperf:ab {here_commit} beside {base_commit}: {said}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
