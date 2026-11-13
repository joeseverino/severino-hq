"""`mise run perf <pages>... [--image] [--seed <name>] [--base <ref>]`: how light and fast HQ is.

Measures the pages asked on a server of this checkout, seeded with a named data
set, so a change's cost is a number, and with a base tells a page's move from
noise. What it measures and its flags: docs/Development.md. A report, never a
gate.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

lazy import django
lazy from django.core.management import call_command
lazy from django.test import override_settings
lazy from playwright.sync_api import sync_playwright

from hq.platform.core.attribution import from_the_shell
from hq.platform.core.tests import task
from hq.platform.core.tests.browser.in_flight import SETTLED
from hq.platform.core.tests.task import ESTATE, PageRefused, RefError, estate_name, trusted_mise, usage, worktree
from tools.perf import measure, parse, runs, table
from tools.perf.targets import (
    COLD_STARTS,
    IMAGE_MEMORY,
    MISE_STATE,
    SCRATCH_MEMORY,
    Image,
    NotMeasured,
    Scratch,
    clean_env,
    run,
)
lazy from hq.platform.core import seeds
lazy from hq.platform.core.tests.browser.estate import Redirected, ask_as_production, landing, navigation, owner_client
lazy from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE, viewport

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var" / "perf"
# The extension set dev:scratch composes, gitignored.
LOCAL = ROOT / "mise.local.toml"
# Set when a run starts again host alone, to the refusal that made it.
ALONE_BECAUSE = "PERF_HOST_ALONE_BECAUSE"
# What perf says of a page asked that the base answers 404 and this tree serves.
NOT_AT_BASE = "not at the base, so measured only in the tree that asked"


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
        """What is served: a composed image's memory and cold start are not the host image's, so a
        kept report of one never stands in for the other."""

        if not self.image:
            return "dev:scratch"
        return "image:composed" if self.extensions else "image"

    def key(self) -> dict[str, Any]:
        """What a kept report must have been asked to stand in for a measurement under this plan."""

        return {
            "target": self.target,
            "pages": self.pages,
            "widths": [shown["width"] for shown in self.viewports],
            "requests": self.requests,
            "seed": self.seed,
            "extensions": self.extensions,
            "browser_waits_for": SETTLED,
            "memory": IMAGE_MEMORY if self.image else SCRATCH_MEMORY,
        }


class Unservable(Exception):
    """A page or data set asked for that perf cannot serve."""


class NothingServed(Exception):
    """Every page asked was left out as one perf cannot serve: a run that measures nothing."""


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


def landed(seed: str, every: list[str], chosen: list[str]) -> tuple[list[str], list[str]]:
    """``every`` and ``chosen`` as the pages they land on, asked of ``seed`` built in this
    process's own throwaway database: a server measured answers each with the page, never a
    redirect to it, so a page's numbers are that page's."""

    call_command("migrate", verbosity=0, interactive=False)
    with owner_client(seed) as client:

        def ask(address: str) -> Any:
            return ask_as_production(client, address)

        try:
            lands = {page: landing(ask, page) for page in dict.fromkeys([*every, *chosen])}
        except Redirected as error:
            raise Unservable(f"{error}, so perf has no page of HQ's to measure.") from None
    for page in chosen:
        if lands[page] != page:
            print(f"{estate_name(page)} lands on {estate_name(lands[page])}, which is measured")
    return [*dict.fromkeys(lands[page] for page in every)], [*dict.fromkeys(lands[page] for page in chosen)]


def served(
    names: tuple[str, ...], every: list[str], *, leave_out: bool, say: Callable[[str], None] = print
) -> list[str]:
    """The addresses of the pages ``names`` asks that perf serves. A name it cannot serve refuses
    the run, or with ``leave_out`` is named through ``say`` and left out, so a caller asking pages
    of every kind (dev:look's) measures those it can."""

    # A bare word dev:look takes as a gate page by name (connections) ends an
    # estate page's path too, and perf knows no gate page, so it takes no word.
    try:
        return task.pages(names, every, by_word=False)
    except PageRefused as refused:
        if not leave_out:
            raise Unservable(f"{refused} perf serves the {ESTATE}'s pages alone.") from None
    chosen = []
    for name in names:
        try:
            chosen += task.pages([name], every, by_word=False)
        except PageRefused:
            say(f"perf leaves out {name}: it serves the {ESTATE}'s pages by path or route name alone")
    if not chosen:
        raise NothingServed("perf: no page asked is one it serves, so nothing is measured")
    return list(dict.fromkeys(chosen))


def set_up() -> str:
    """Django set up with the pages the servers measured will compose, and why the host is
    measured alone, as ``alone_because``."""

    alone = alone_because()
    if alone:
        # The pages are the host's own, as the servers measured will compose them.
        os.environ["SEVERINO_HQ_PLUGINS"] = ""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    django.setup()
    return alone


def _unsaid(_said: str) -> None:
    """What the early check leaves to the run to say."""


def refuse_early() -> None:
    """Check the data set and the pages asked before the task waits for the heavy turn, exiting 2
    with the refusal ``plan`` would make once it holds it. Neither needs a database: a page is
    found among the navigation's addresses, and only where it lands is asked of the seed."""

    set_up()
    asked = usage()
    try:
        seed = seed_of(asked.text("seed"))
        with override_settings(**seeds.get(seed).settings):
            served(asked.repeated("pages"), navigation(), leave_out=asked.switch("skip_unservable"), say=_unsaid)
    except Unservable as error:
        print(f"perf: {error}", file=sys.stderr)
        sys.exit(2)
    except NothingServed:
        # The run says so: --skip-unservable is dev:look's, which runs perf inside its own turn.
        return


def plan() -> Plan:
    """The pages asked, the phone's and the wide desktop's viewports, and the task's flags."""

    alone = set_up()
    asked = usage()
    seed = seed_of(asked.text("seed"))
    # The pages the set's navigation reaches, and those asked, under the settings it is shown with.
    with override_settings(**seeds.get(seed).settings):
        every = navigation()
        chosen = served(asked.repeated("pages"), every, leave_out=asked.switch("skip_unservable"))
        # Seeded in this process rather than through manage.py, so its door binds here.
        with from_the_shell("perf.report.plan"):
            every, chosen = landed(seed, every, chosen)
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


def image_of(tree: Path, commit: str, *, here: bool, composed: bool) -> str:
    """The image to run for ``tree``: the one IMAGE hands over here, else the host image
    image:build makes, composed with this checkout's extension set where the plan composes one,
    as production runs it. A base tree has no image:compose of its own, so this checkout's
    composes it with the base's own composition/Dockerfile and staging."""

    if here and os.environ.get("IMAGE"):
        return os.environ["IMAGE"]
    host = f"severino-hq:perf-{commit}"
    run(
        ["mise", "-C", str(tree), "run", "image:build", "--tag", host],
        env=clean_env(**({} if here else trusted_mise(tree, tree.parent / MISE_STATE))),
    )
    if not composed:
        return host
    tag = f"{host}-composed"
    command = ["mise", "-C", str(ROOT), "run", "image:compose", "--host", host, "--tag", tag, "--context", str(tree)]
    done = subprocess.run(command, capture_output=True, text=True, env=clean_env(), check=False)
    if done.returncode:
        said = f"{done.stdout}\n{done.stderr}"
        # The build collects static under the set, so a plugin API's refusal surfaces here, above
        # the build's own closing lines, and serve() reads it to measure the host alone.
        tail = "\n".join(said.strip().splitlines()[-20:])
        raise NotMeasured(f"image:compose exited {done.returncode}: {parse.api_refusal(said) or tail}")
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
    # Each page the first pass walked, by its status.
    answered: dict[str, int] = field(default_factory=dict)
    stopped: bool = False

    def entry(self, page: str) -> dict[str, Any]:
        return next(entry for entry in self.recorder.report["pages"] if entry["page"] == page)

    def pages(self, asked: Plan) -> list[str]:
        """The pages asked that this side has, so a page the base predates is never measured there."""

        return [page for page in asked.pages if not self.entry(page).get("absent")]

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


def unanswered(live: dict[str, Live], asked: Plan) -> None:
    """Each page asked that a side answered anything but 200, recorded as a failure, since its
    numbers measure no page; but a page the base answers 404 and this tree serves is one the tree
    adds, said so and kept out of the base's measurements, as dev:look's base leaves it out."""

    here = live["here"].answered if "here" in live else {}
    for side, server in live.items():
        for page in asked.pages:
            status = server.answered[page]
            if status == 200:
                continue
            if side == "base" and status == 404 and here.get(page) == 200:
                server.entry(page)["absent"] = True
                server.recorder.save()
                print(f"{estate_name(page)}: {NOT_AT_BASE}", flush=True)
                continue
            server.recorder.fail(f"page {estate_name(page)}", NotMeasured(f"{page} answered {status}, not 200"))


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

    pages = server.pages(asked)

    def browser() -> None:
        if not pages:
            return
        with chromium() as launched:
            costs = measure.browser_costs(launched, target.url, server.cookie, pages, asked.viewports)
        for page, seen in costs.items():
            server.entry(page)["browser"] = seen

    def queries() -> None:
        for page, count in target.queries(pages).items():
            server.entry(page)["server"]["queries"] = count

    report["load"] = runs.load()
    print(f"Measuring {report['commit']} at load {report['load']}: cold start, {COLD_STARTS} times", flush=True)
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


def serve(
    tree: Path, commit: str, *, here: bool, asked: Plan, kept: runs.Run, side: str, stack: ExitStack
) -> Live | None:
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
            Image(image_of(tree, commit, here=here, composed=bool(asked.extensions)), log, asked.seed)
            if asked.image
            else Scratch(tree, log, asked.seed, trusted=not here, alone=bool(asked.alone))
        )
        with recorder.timed("start"):
            target.__enter__()
    except Exception as error:
        recorder.fail("start", error)
        said = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        # A composed image's build can refuse the set before any server runs.
        said += f"\n{error}"
        refused = None if asked.alone else parse.api_refusal(said)
        if refused:
            raise SetRefused(f"{commit} refused the extension set: {refused}") from error
        return None
    server = Live(recorder, target)
    stack.callback(server.stop)
    try:
        with recorder.timed("sign in"):
            server.cookie = target.session()
        server.answered = first_pass(server, asked)
    except NotMeasured as error:
        recorder.fail("sign in and first pass", error)
        return None
    return server


def measure_server(live: dict[str, Live], asked: Plan) -> None:
    """Every live side's server time, its servers asked in turn, and beside a base each page's
    move from the base's with its spread, kept in this checkout's report."""

    together = " in turn" if len(live) > 1 else ""
    print(f"Server time over {asked.requests} rounds of every page, asking {', '.join(live)}{together}", flush=True)
    started = time.monotonic()
    try:
        found = measure.server_times(
            {side: (server.target.url, server.cookie) for side, server in live.items()},
            asked.pages,
            asked.requests,
            lacks={side: set(asked.pages) - set(server.pages(asked)) for side, server in live.items()},
        )
    except Exception as error:  # noqa: BLE001 - recorded on every side it was asked of, and the run goes on
        for server in live.values():
            server.recorder.fail("server time", error)
        return
    for side, pages in found.items():
        for page, numbers in pages.items():
            live[side].entry(page)["server"].update(numbers)
    if {"base", "here"} <= found.keys():
        for page in found["base"]:
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
    """How many pages' server time moved from the base's past its spread, and which; a page the
    base lacks has no move, so it is not counted."""

    compared = [entry for entry in here["pages"] if entry["server"].get("moved_ms") is not None]
    moved = [entry["page"] for entry in compared if measure.past_spread(entry["server"])]
    said = f"{len(moved)} of {len(compared)} pages moved past their spread"
    return said + (f" ({', '.join(moved)})" if moved else "")


def closing(kept: runs.Run, here: runs.Report, base: runs.Report | None) -> str:
    said = f"perf {here['commit']}" + (f" beside {base['commit']}" if base else "")
    said += f" ({here['target']}, {kept.doc['plan']['seed']})"
    if kept.doc.get("alone"):
        said += f", the host measured alone{' on both sides' if base else ''} because {kept.doc['alone'].rstrip('.')}"
    if reused := kept_from(kept.doc):
        said += f", {reused}"
    if base:
        said += f": {verdict(here)};"
        if absent := [estate_name(entry["page"]) for entry in base["pages"] if entry.get("absent")]:
            said += f" {', '.join(absent)} {NOT_AT_BASE};"
    else:
        said += ":"
    failed = failures(kept.doc)
    said += f" {len(failed)} failed, the rest kept ({'; '.join(failed)})" if failed else " every measurement finished"
    return said + f"; kept in {kept.path.relative_to(ROOT)}"


def stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def kept_from(doc: dict[str, Any]) -> str | None:
    """Which run the base's numbers but its server time are kept from, and both sides' loads, or
    None when this run measured them."""

    if not (reused := doc.get("base_reused_from")):
        return None
    said = f"the base's browser, memory and cold start kept from run {reused}, at load {doc['base']['load']}"
    here = (doc.get("here") or {}).get("load")
    return said + (f" beside this run's {here}" if here is not None else ", this run's unmeasured")


def kept_base(commit: str, asked: Plan, kept: runs.Run) -> bool:
    """Whether the base's numbers but its server time come from a complete report of these pages
    kept under the same plan and a load alike the load now, taken into this run's base."""

    now = runs.load()
    if not (found := runs.saved(OUT, commit, asked.key(), now)):
        print(f"Base {commit}: no complete run of it kept under this plan within load {runs.LOAD_BOUND} of {now}")
        return False
    source, report = found
    said = f"Base {commit}: its browser, memory and cold start from run {source}, at load {report['load']}"
    print(f"{said} (--fresh measures them)")
    runs.stand_in(kept.doc["base"], report)
    kept.doc["base_reused_from"] = source
    kept.save()
    return True


def measure_run(base_commit: str | None, here_commit: str, asked: Plan, kept: runs.Run, *, fresh: bool) -> None:
    """Both servers started, their server time asked of both in turn, then each side's other
    numbers; every server stopped and the base's worktree gone however the block ends."""

    with ExitStack() as stack:
        live = {}
        if base_commit:
            live["base"] = serve(
                stack.enter_context(base_tree(base_commit)),
                base_commit[:12],
                here=False,
                asked=asked,
                kept=kept,
                side="base",
                stack=stack,
            )
        live["here"] = serve(ROOT, here_commit, here=True, asked=asked, kept=kept, side="here", stack=stack)
        serving = {side: server for side, server in live.items() if server}
        unanswered(serving, asked)
        # First, while every server is as its first boot and one pass left it, so neither has a
        # history the other lacks.
        if serving:
            measure_server(serving, asked)
        # Whether a kept base stands in is decided under the load here's numbers are measured
        # under next, after the builds and boots, which load the machine more than measuring does.
        measuring = set(serving)
        if base_commit and not fresh and kept_base(base_commit[:12], asked, kept):
            measuring.discard("base")
        # One server at a time, so the other's work is not in its numbers.
        for side, server in serving.items():
            if side in measuring:
                measure_costs(server, asked)
            server.stop()
    for side in live:
        kept.doc[side]["finished_at"] = runs.now()
    kept.save()


def main() -> int:
    """0 when every measurement finished, or --skip-unservable left out every page asked; 1 when
    one failed or a page answered anything but 200; 2 for a ref that names no commit, or a page or
    set it cannot serve."""

    signal.signal(signal.SIGTERM, stop)
    ref = usage().text("base")
    try:
        asked = plan()
    except Unservable as error:
        print(f"perf: {error}", file=sys.stderr)
        return 2
    except NothingServed as said:
        print(said, flush=True)
        return 0
    here_commit = commit_of(ROOT)
    try:
        base_commit = task.commit_of(ref) if ref else None
    except RefError as error:
        print(f"perf --base {ref}: {error}", file=sys.stderr)
        return 2
    stem = runs.name(here_commit, base_commit and base_commit[:12], asked.target, datetime.now(UTC))
    kept = runs.Run.start(OUT, stem, target=asked.target, plan=asked.key(), alone=asked.alone or None)
    try:
        measure_run(base_commit, here_commit, asked, kept, fresh=usage().switch("fresh"))
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
