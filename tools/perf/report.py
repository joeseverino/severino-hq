"""`mise run perf:report <pages>... [--image] [--seed <name>] [--base <ref>]`: how light and fast HQ is.

Measures the pages asked, named as dev:look names the example estate's
(`hq.platform.core.tests.task.pages`), or all for every page its navigation lists, on this
checkout's `dev:scratch`, or its built image, seeded with a named data set, so
a change's cost is a number. With a base, both servers stay up and are asked
for server time in turn, so a page's move is told from noise
(`measure.server_times`). What it measures and its flags are in
docs/Development.md. A report, never a gate: it exits 0 when every measurement
finished, 1 when any failed or a page answered anything but 200, 2 for a ref
that names no commit, or a page or set it cannot serve.
"""

import copy
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

lazy import django
lazy from django.test import override_settings
lazy from playwright.sync_api import sync_playwright

from hq.platform.core.tests import task
from hq.platform.core.tests.task import ESTATE, PageRefused, RefError, estate_name, usage, worktree
from tools.perf import measure, parse, runs, table
from tools.perf.targets import COLD_STARTS, Image, NotMeasured, Scratch, clean_env, run
lazy from hq.platform.core import seeds
lazy from hq.platform.core.tests.browser.estate import navigation
lazy from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE, viewport

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var" / "perf"
# The extension set dev:scratch composes, gitignored.
LOCAL = ROOT / "mise.local.toml"
# Set when a run starts again host alone, to the refusal that made it.
ALONE_BECAUSE = "PERF_HOST_ALONE_BECAUSE"


@dataclass(frozen=True)
class Plan:
    """What every measured tree is asked, alike."""

    pages: list[str]
    # Every page the navigation lists and the pages asked: the pass memory is read after.
    walked: list[str]
    viewports: list[dict[str, int]]
    requests: int
    image: bool
    # The data set every server measured is seeded with.
    seed: str
    extensions: list[str]
    # Why the host is measured with no extension composed, or nothing when the set is composed.
    alone: str = ""

    @property
    def target(self) -> str:
        return "image" if self.image else "dev:scratch"

    def key(self) -> dict[str, Any]:
        """What a kept report must have been asked to stand in for a measurement under this plan."""

        return {
            "target": self.target,
            "pages": self.pages,
            "widths": [shown["width"] for shown in self.viewports],
            "requests": self.requests,
            "seed": self.seed,
            "extensions": self.extensions,
            "browser_waits_for": measure.SETTLED,
        }


class Unservable(Exception):
    """A page or data set asked for that perf:report cannot serve."""


class SetRefused(Exception):
    """A tree's server refused the extension set for the plugin API it supports."""


def alone_because() -> str:
    """Why this run measures the host alone, or nothing when it composes the extension set."""

    if because := os.environ.get(ALONE_BECAUSE):
        return because
    return "--host-alone asked for it" if usage().switch("host_alone") else ""


def seed_of(asked: str) -> str:
    """The data set ``asked`` names. A stress set registers beside its builder in the test tree,
    which no server imports, so the sets a server can be seeded with are the registry's own."""

    if asked not in seeds.SEEDS:
        raise Unservable(f"No data set {asked!r} a server can be seeded with. Sets: {', '.join(seeds.SEEDS)}")
    return asked


def plan() -> Plan:
    """The pages asked, the phone's and the wide desktop's viewports, and the task's flags."""

    alone = alone_because()
    if alone:
        # The pages are the host's own, as the servers measured will compose them.
        os.environ["SEVERINO_HQ_PLUGINS"] = ""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    django.setup()
    asked = usage()
    seed = seed_of(asked.text("seed"))
    # The pages the set's navigation reaches, and those asked, under the settings it is shown with.
    with override_settings(**seeds.get(seed).settings):
        every = navigation()
        try:
            chosen = task.pages(asked.repeated("pages"), every)
        except PageRefused as refused:
            raise Unservable(f"{refused} perf:report serves the {ESTATE}'s pages alone.") from None
    return Plan(
        pages=chosen,
        walked=list(dict.fromkeys([*every, *chosen])),
        viewports=[viewport(PHONE_SCREEN[0]), viewport(WIDE)],
        requests=int(asked.text("requests")),
        image=asked.switch("image"),
        seed=seed,
        extensions=[spec for spec in os.environ.get("SEVERINO_HQ_PLUGINS", "").split(",") if spec],
        alone=alone,
    )


def commit_of(tree: Path, ref: str = "HEAD") -> str:
    """The commit, short, and for a checkout holding changes past it, marked so."""

    sha = run(["git", "-C", str(tree), "rev-parse", "--short=12", ref]).strip()
    changed = ref == "HEAD" and run(["git", "-C", str(tree), "status", "--porcelain"]).strip()
    return f"{sha}-changed" if changed else sha


@contextmanager
def chromium() -> Iterator[Any]:
    """Chromium, the engine that reports layout shifts and long tasks; this module is the one the
    task runs under the browser group, so it alone imports Playwright."""

    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()


@contextmanager
def base_tree(commit: str) -> Iterator[Path]:
    """A worktree of ``commit`` with this checkout's mise.local.toml, so its server composes the same extensions."""

    with worktree(commit) as tree:
        if LOCAL.is_file():
            shutil.copy2(LOCAL, tree / LOCAL.name)
        yield tree


def image_of(tree: Path, commit: str, here: bool) -> str:
    """The image to run for ``tree``: the one IMAGE hands over here, else one image:build makes."""

    if here and os.environ.get("IMAGE"):
        return os.environ["IMAGE"]
    tag = f"severino-hq:perf-{commit}"
    run(
        ["mise", "-C", str(tree), "run", "image:build", "--tag", tag],
        env=clean_env(MISE_TRUSTED_CONFIG_PATHS=str(tree)),
    )
    return tag


@dataclass
class Recorder:
    """One side's report, saved with its run each time a measurement finishes or fails."""

    report: runs.Report
    save: Callable[[], None]

    @contextmanager
    def timed(self, step: str) -> Iterator[None]:
        """How long ``step`` took, kept in the report's ``took_s`` and said as it ends."""

        started = time.monotonic()
        try:
            yield
        finally:
            took = self.report.setdefault("took_s", {})[step] = round(time.monotonic() - started, 1)
            print(f"  {step}: {took}s", flush=True)

    def fail(self, step: str, error: BaseException) -> None:
        said = str(error).strip() or type(error).__name__
        self.report["failures"].append({"step": step, "error": said})
        print(f"  {step} failed, the rest goes on: {said.splitlines()[0]}", flush=True)
        self.save()

    def attempt(self, step: str, measured: Callable[[], None]) -> None:
        """``measured``, which writes its numbers into the report; its failure is recorded, never raised."""

        try:
            with self.timed(step):
                measured()
        except Exception as error:  # noqa: BLE001 - whatever stops one measurement is recorded, and the rest go on
            self.fail(step, error)
            return
        self.save()


@dataclass
class Live:
    """One side's server, up and signed in."""

    recorder: Recorder
    target: Scratch | Image
    cookie: tuple[str, str] = ("", "")
    stopped: bool = False

    def entry(self, page: str) -> dict[str, Any]:
        return next(entry for entry in self.recorder.report["pages"] if entry["page"] == page)

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        try:
            self.target.__exit__(None, None, None)
        except Exception as error:  # noqa: BLE001 - what was measured stands whether or not the server stops cleanly
            self.recorder.fail("stop", error)


def first_pass(server: Live, asked: Plan) -> dict[str, int]:
    """Every page the navigation lists and the pages asked, once, by status: what memory is read
    after, and what every later measurement stands on, so its failure raises."""

    with server.recorder.timed("first pass"):
        answered = measure.one_pass(server.target.url, server.cookie, asked.walked)
    if 200 not in answered.values():
        raise NotMeasured(f"no page answered 200 to the session dev_session minted: {answered}")
    return answered


def unanswered(server: Live, asked: Plan, answered: dict[str, int]) -> None:
    """Each page asked that answered anything but 200, recorded as a failure: its numbers measure no page."""

    for page in asked.pages:
        if answered[page] != 200:
            failed = NotMeasured(f"{page} answered {answered[page]}, not 200")
            server.recorder.fail(f"page {estate_name(page)}", failed)


def measure_costs(server: Live, asked: Plan) -> None:
    """Every number for one side but its server time, measured before it, in the order that keeps
    each clean: the cold starts first, so the server measured runs from the bytecode its first boot
    wrote, as a checkout that has run before does (a base's new worktree compiles every module on
    that boot), then memory before any page."""

    report, target, recorder = server.recorder.report, server.target, server.recorder
    memory = report["memory"] = {"idle_bytes": None, "after_pass_bytes": None, "measured": target.measured}

    def cold_start() -> None:
        starts = [round(target.cold_start(), 1) for _ in range(COLD_STARTS)]
        report["cold_start"] = {"runs_ms": starts, "median_ms": sorted(starts)[len(starts) // 2]}

    def browser() -> None:
        with chromium() as launched:
            costs = measure.browser_costs(launched, target.url, server.cookie, asked.pages, asked.viewports)
        for page, seen in costs.items():
            server.entry(page)["browser"] = seen

    def queries() -> None:
        for page, count in target.queries(asked.pages).items():
            server.entry(page)["server"]["queries"] = count

    print(f"Measuring {report['commit']}: cold start, {COLD_STARTS} times", flush=True)
    recorder.attempt("cold start", cold_start)
    recorder.attempt("memory idle", lambda: memory.update(idle_bytes=target.memory()))
    try:
        first_pass(server, asked)
    except NotMeasured as error:
        recorder.fail("first pass", error)
        return
    recorder.attempt("memory after a pass", lambda: memory.update(after_pass_bytes=target.memory()))
    print("  in Chromium", flush=True)
    recorder.attempt("browser", browser)
    recorder.attempt("queries", queries)


def serve(tree: Path, commit: str, here: bool, asked: Plan, kept: runs.Run, side: str, stack: ExitStack) -> Live | None:
    """One tree's server, started, signed in and walked once, its report under ``side``, and
    stopped when ``stack`` closes if not before. None where it would not start or sign in,
    recorded as a failure."""

    if side not in kept.doc:
        kept.doc[side] = runs.side(commit, asked.key())
        kept.doc[side]["pages"] = [{"page": page, "server": {}, "browser": {}} for page in asked.pages]
    recorder = Recorder(kept.doc[side], kept.save)
    kept.save()
    log = OUT / f"{kept.name}.{side}.log"
    print(f"Starting {commit} ({asked.target}, {asked.seed}); its server's output is in {log}", flush=True)
    try:
        target = (
            Image(image_of(tree, commit, here), log, asked.seed)
            if asked.image
            else Scratch(tree, log, asked.seed, trusted=not here, alone=bool(asked.alone))
        )
        with recorder.timed("start"):
            target.__enter__()
    except Exception as error:
        recorder.fail("start", error)
        said = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        refused = None if asked.alone else parse.api_refusal(said)
        if refused:
            raise SetRefused(f"{commit} refused the extension set: {refused}") from error
        return None
    server = Live(recorder, target)
    stack.callback(server.stop)
    try:
        with recorder.timed("sign in"):
            server.cookie = target.session()
        answered = first_pass(server, asked)
    except NotMeasured as error:
        recorder.fail("sign in and first pass", error)
        return None
    unanswered(server, asked, answered)
    return server


def measure_server(live: dict[str, Live], asked: Plan) -> None:
    """Every live side's server time, its servers asked in turn, and beside a base each page's
    move from the base's with its spread, kept in this checkout's report."""

    together = " in turn" if len(live) > 1 else ""
    print(f"Server time over {asked.requests} rounds of every page, asking {', '.join(live)}{together}", flush=True)
    started = time.monotonic()
    try:
        found = measure.server_times(
            {side: (server.target.url, server.cookie) for side, server in live.items()}, asked.pages, asked.requests
        )
    except Exception as error:  # noqa: BLE001 - recorded on every side it was asked of, and the run goes on
        for server in live.values():
            server.recorder.fail("server time", error)
        return
    for side, pages in found.items():
        for page, numbers in pages.items():
            live[side].entry(page)["server"].update(numbers)
    if {"base", "here"} <= found.keys():
        for page in asked.pages:
            difference = measure.moved(found["base"][page]["times_ms"], found["here"][page]["times_ms"])
            live["here"].entry(page)["server"].update(difference or {})
    took = round(time.monotonic() - started, 1)
    for server in live.values():
        server.recorder.report.setdefault("took_s", {})["server time"] = took
        server.recorder.save()
    print(f"  server time: {took}s", flush=True)


def failures(doc: dict[str, Any]) -> list[str]:
    """Each failure in the run, as its side, its step and the first line of its error."""

    return [
        f"{side} {failure['step']}: {failure['error'].splitlines()[0]}"
        for side in ("base", "here")
        for failure in (doc.get(side) or {}).get("failures", [])
    ]


def verdict(here: runs.Report) -> str:
    """How many pages' server time moved from the base's past its spread, and which."""

    compared = [entry for entry in here["pages"] if entry["server"].get("moved_ms") is not None]
    moved = [entry["page"] for entry in compared if measure.past_spread(entry["server"])]
    said = f"{len(moved)} of {len(compared)} pages moved past their spread"
    return said + (f" ({', '.join(moved)})" if moved else "")


def closing(kept: runs.Run, here: runs.Report, base: runs.Report | None) -> str:
    said = f"perf:report {here['commit']}" + (f" beside {base['commit']}" if base else "")
    said += f" ({here['target']}, {kept.doc['plan']['seed']})"
    if kept.doc.get("alone"):
        said += f", the host measured alone{' on both sides' if base else ''} because {kept.doc['alone'].rstrip('.')}"
    if reused := kept.doc.get("base_reused_from"):
        said += f", the base's browser, memory and cold start kept from run {reused}"
    if base:
        said += f": {verdict(here)};"
    else:
        said += ":"
    failed = failures(kept.doc)
    said += f" {len(failed)} failed, the rest kept ({'; '.join(failed)})" if failed else " every measurement finished"
    return said + f"; kept in {kept.path.relative_to(ROOT)}"


def stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def kept_base(commit: str, asked: Plan, kept: runs.Run) -> bool:
    """Whether the base's numbers but its server time come from a complete report of these pages
    kept under the same plan, taken into this run."""

    if not (found := runs.saved(OUT, commit, asked.key())):
        return False
    source, report = found
    print(f"Base {commit}: its browser, memory and cold start from run {source} (--fresh measures them)")
    kept.doc["base"] = copy.deepcopy(report)
    kept.doc["base_reused_from"] = source
    kept.save()
    return True


def measure_run(base_commit: str | None, here_commit: str, asked: Plan, kept: runs.Run, fresh: bool) -> None:
    """Both servers started, their server time asked of both in turn, then each side's other
    numbers; every server stopped and the base's worktree gone however the block ends."""

    with ExitStack() as stack:
        live = {}
        measuring = {"here"}
        if base_commit:
            short = base_commit[:12]
            if fresh or not kept_base(short, asked, kept):
                measuring.add("base")
            live["base"] = serve(stack.enter_context(base_tree(base_commit)), short, False, asked, kept, "base", stack)
        live["here"] = serve(ROOT, here_commit, True, asked, kept, "here", stack)
        serving = {side: server for side, server in live.items() if server}
        # First, while every server is as its first boot and one pass left it, so neither has a
        # history the other lacks.
        if serving:
            measure_server(serving, asked)
        # One server at a time, so the other's work is not in its numbers.
        for side, server in serving.items():
            if side in measuring:
                measure_costs(server, asked)
            server.stop()
    for side in live:
        kept.doc[side]["finished_at"] = runs.now()
    kept.save()


def main() -> int:
    signal.signal(signal.SIGTERM, stop)
    ref = usage().text("base")
    try:
        asked = plan()
    except Unservable as error:
        print(f"perf:report: {error}", file=sys.stderr)
        return 2
    here_commit = commit_of(ROOT)
    try:
        base_commit = task.commit_of(ref) if ref else None
    except RefError as error:
        print(f"perf:report --base {ref}: {error}", file=sys.stderr)
        return 2
    stem = runs.name(here_commit, base_commit and base_commit[:12], asked.target, datetime.now(UTC))
    kept = runs.Run.start(OUT, stem, target=asked.target, plan=asked.key(), alone=asked.alone or None)
    try:
        measure_run(base_commit, here_commit, asked, kept, usage().switch("fresh"))
    except SetRefused as refused:
        kept.doc["refused"] = str(refused)
        kept.save()
        print(f"{refused}; measuring the host alone on both sides instead", flush=True)
        # The page list comes from this process's Django, composed with the set, so the run starts again.
        os.execve(
            sys.executable, [sys.executable, "-m", "tools.perf.report"], os.environ | {ALONE_BECAUSE: str(refused)}
        )
    here, base = kept.doc["here"], kept.doc.get("base")
    rendered = table.render(here, base)
    every_page = kept.path.with_suffix(".table")
    every_page.write_text(rendered + "\n", encoding="utf-8")
    # The session log shows each var/perf/*.txt whole, so the summary is the .txt and the table is not.
    summary = table.summary(here, base, str(every_page.relative_to(ROOT)))
    said = closing(kept, here, base)
    kept.path.with_suffix(".txt").write_text(f"{summary}\n\n{said}\n", encoding="utf-8")
    print(rendered, "", *table.moved(here, base), "", said, sep="\n")
    return 1 if failures(kept.doc) else 0


if __name__ == "__main__":
    sys.exit(main())
