"""`mise run perf:report [--image] [--base <ref>]`: how light and fast HQ is.

Boots this checkout's `dev:scratch` seeded with the showcase estate (or, with
`--image`, its built image), signs in as dev_session does, and measures every
page the layout survey walks (`estate.navigation`): the server's time, p50
and p95 over `--requests` requests, and its queries; in Chromium at the phone
and the wide desktop, bytes and requests on a first and a repeat visit, main
thread script time, long tasks and layout shift; memory idle and after one
pass over every page; and the cold start to /health/ready/.

Each run is kept in var/perf/<run>.json (tools/perf/runs.py), rewritten as
each measurement finishes: one that fails is recorded with its error and the
rest go on, and a server that fails to stop keeps what it served. The table
of every page goes beside it as <run>.table, and its summary (the totals,
memory, cold start and the pages that moved most) as <run>.txt, which the
session log shows.

`--host-alone` measures the host with no extension composed. A ref whose
server refuses the extension set for the plugin API it supports (its log
holds HQ's refusal) makes the run start again host alone on both sides, and
its closing line says so and why.

`--base <ref>` measures the ref first, its own server in a throwaway worktree
made and removed as `mise run test --at` makes one (tools/gate/at.py), with
this checkout's harness and page list, and prints each number beside how far
it moved. A complete report of the ref's commit under the same plan, kept by
an earlier run, stands in for measuring it again unless `--fresh`. A report,
never a gate: it exits 0 when every measurement finished, 1 when any failed,
2 for a ref that names no commit.
"""

import copy
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

lazy import django
lazy from playwright.sync_api import sync_playwright

from tools.gate.at import RefError, git, remove
from tools.perf import measure, parse, runs, table
from tools.perf.targets import COLD_STARTS, Image, NotMeasured, Scratch, clean_env, run
lazy from hq.platform.core.tests.browser.estate import navigation
lazy from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE, viewport

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var" / "perf"
# The extension set dev:scratch composes, gitignored.
LOCAL = ROOT / "mise.local.toml"
# Set when a run starts again host alone, to the refusal that made it.
ALONE_BECAUSE = "PERF_HOST_ALONE_BECAUSE"


def usage(name: str) -> str:
    """A task argument, as mise exports it: ``usage_<name>``, nothing for an unset flag."""

    return os.environ.get(f"usage_{name}", "")


@dataclass(frozen=True)
class Plan:
    """What every measured tree is asked, alike."""

    pages: list[str]
    viewports: list[dict[str, int]]
    requests: int
    image: bool
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
            "extensions": self.extensions,
        }


class SetRefused(Exception):
    """A tree's server refused the extension set for the plugin API it supports."""


def alone_because() -> str:
    """Why this run measures the host alone, or nothing when it composes the extension set."""

    if because := os.environ.get(ALONE_BECAUSE):
        return because
    return "--host-alone asked for it" if usage("host_alone") == "true" else ""


def plan() -> Plan:
    """The pages the layout survey walks, the phone's and the wide desktop's viewports, and the task's flags."""

    alone = alone_because()
    if alone:
        # The pages are the host's own, as the servers measured will compose them.
        os.environ["SEVERINO_HQ_PLUGINS"] = ""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    django.setup()
    return Plan(
        pages=navigation(),
        viewports=[viewport(PHONE_SCREEN[0]), viewport(WIDE)],
        requests=int(usage("requests") or 20),
        image=usage("image") == "true",
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
def worktree(commit: str) -> Iterator[Path]:
    """A detached worktree of ``commit`` outside this checkout, so mise reads only its config, with this
    checkout's mise.local.toml, so its server composes the same extensions; gone however the block ends."""

    folder = Path(tempfile.mkdtemp(prefix="hq-perf-base-"))
    tree = folder / "checkout"
    try:
        git("worktree", "add", "--detach", "--quiet", str(tree), commit)
        if LOCAL.is_file():
            shutil.copy2(LOCAL, tree / LOCAL.name)
        yield tree
    finally:
        remove(tree)
        shutil.rmtree(folder, ignore_errors=True)


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

    def fail(self, step: str, error: BaseException) -> None:
        said = str(error).strip() or type(error).__name__
        self.report["failures"].append({"step": step, "error": said})
        print(f"  {step} failed, the rest goes on: {said.splitlines()[0]}", flush=True)
        self.save()

    def attempt(self, step: str, measured: Callable[[], None]) -> None:
        """``measured``, which writes its numbers into the report; its failure is recorded, never raised."""

        try:
            measured()
        except Exception as error:  # noqa: BLE001 - whatever stops one measurement is recorded, and the rest go on
            self.fail(step, error)
            return
        self.save()


def measure_target(target: Scratch | Image, asked: Plan, recorder: Recorder) -> None:
    """Every number for one started target, in the order that keeps each clean: memory before any page.

    Signing in and the first pass are what every later measurement stands on, so their failure raises."""

    report = recorder.report
    pages = asked.pages
    entries = {page: {"page": page, "server": {}, "browser": {}} for page in pages}
    report["pages"] = list(entries.values())
    memory = report["memory"] = {"idle_bytes": None, "after_pass_bytes": None, "measured": target.measured}
    cookie = target.session()
    recorder.attempt("memory idle", lambda: memory.update(idle_bytes=target.memory()))
    answered = measure.one_pass(target.url, cookie, pages)
    if 200 not in answered.values():
        raise NotMeasured(f"no page answered 200 to the session dev_session minted: {answered}")
    recorder.attempt("memory after a pass", lambda: memory.update(after_pass_bytes=target.memory()))

    def server() -> None:
        for page, numbers in measure.server_times(target.url, cookie, pages, asked.requests).items():
            entries[page]["server"].update(numbers)

    def browser() -> None:
        with chromium() as launched:
            costs = measure.browser_costs(launched, target.url, cookie, pages, asked.viewports)
        for page, seen in costs.items():
            entries[page]["browser"] = seen

    def queries() -> None:
        for page, count in target.queries(pages).items():
            entries[page]["server"]["queries"] = count

    def cold_start() -> None:
        starts = [round(target.cold_start(), 1) for _ in range(COLD_STARTS)]
        report["cold_start"] = {"runs_ms": starts, "median_ms": sorted(starts)[len(starts) // 2]}

    print(f"  server time over {asked.requests} requests a page", flush=True)
    recorder.attempt("server time", server)
    print("  in Chromium", flush=True)
    recorder.attempt("browser", browser)
    recorder.attempt("queries", queries)
    print(f"  cold start, {COLD_STARTS} times", flush=True)
    recorder.attempt("cold start", cold_start)


def measure_tree(tree: Path, commit: str, here: bool, asked: Plan, kept: runs.Run, side: str) -> runs.Report:
    """One tree's report, kept under ``side`` in its run as each measurement finishes."""

    report = kept.doc[side] = runs.side(commit, asked.key())
    recorder = Recorder(report, kept.save)
    kept.save()
    log = OUT / f"{kept.name}.{side}.log"
    print(f"Measuring {commit} ({asked.target}); its server's output is in {log}", flush=True)
    try:
        target = (
            Image(image_of(tree, commit, here), log)
            if asked.image
            else Scratch(tree, log, trusted=not here, alone=bool(asked.alone))
        )
        target.__enter__()
    except Exception as error:
        recorder.fail("start", error)
        said = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        refused = None if asked.alone else parse.api_refusal(said)
        if refused:
            raise SetRefused(f"{commit} refused the extension set: {refused}") from error
        return report
    try:
        measure_target(target, asked, recorder)
    except NotMeasured as error:
        recorder.fail("sign in and first pass", error)
    finally:
        try:
            target.__exit__(None, None, None)
        except Exception as error:  # noqa: BLE001 - what was measured stands whether or not the server stops cleanly
            recorder.fail("stop", error)
    report["finished_at"] = runs.now()
    kept.save()
    return report


def failures(doc: dict[str, Any]) -> list[str]:
    """Each failure in the run, as its side, its step and the first line of its error."""

    return [
        f"{side} {failure['step']}: {failure['error'].splitlines()[0]}"
        for side in ("base", "here")
        for failure in (doc.get(side) or {}).get("failures", [])
    ]


def closing(kept: runs.Run, here: runs.Report, base: runs.Report | None) -> str:
    said = f"perf:report {here['commit']}" + (f" beside {base['commit']}" if base else "")
    said += f" ({here['target']})"
    if kept.doc.get("alone"):
        said += f", the host measured alone{' on both sides' if base else ''} because {kept.doc['alone'].rstrip('.')}"
    if reused := kept.doc.get("base_reused_from"):
        said += f", the base kept from run {reused}"
    failed = failures(kept.doc)
    said += f": {len(failed)} failed, the rest kept ({'; '.join(failed)})" if failed else ": every measurement finished"
    return said + f"; kept in {kept.path.relative_to(ROOT)}"


def stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def measure_base(commit: str, asked: Plan, kept: runs.Run, fresh: bool) -> runs.Report:
    """The base's report: a complete one kept under the same plan, unless ``fresh``, else measured now."""

    short = commit[:12]
    if not fresh and (found := runs.saved(OUT, short, asked.key())):
        source, report = found
        print(f"Base {short}: a complete report under the same plan, kept by run {source} (--fresh measures it)")
        kept.doc["base"] = copy.deepcopy(report)
        kept.doc["base_reused_from"] = source
        kept.save()
        return kept.doc["base"]
    with worktree(commit) as tree:
        return measure_tree(tree, short, False, asked, kept, "base")


def main() -> int:
    signal.signal(signal.SIGTERM, stop)
    ref = usage("base")
    asked = plan()
    here_commit = commit_of(ROOT)
    try:
        base_commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")[0] if ref else None
    except RefError as error:
        print(f"perf:report --base {ref}: {error}", file=sys.stderr)
        return 2
    stem = runs.name(here_commit, base_commit and base_commit[:12], asked.target, datetime.now(UTC))
    kept = runs.Run.start(OUT, stem, target=asked.target, plan=asked.key(), alone=asked.alone or None)
    try:
        base = measure_base(base_commit, asked, kept, usage("fresh") == "true") if base_commit else None
        here = measure_tree(ROOT, here_commit, True, asked, kept, "here")
    except SetRefused as refused:
        kept.doc["refused"] = str(refused)
        kept.save()
        print(f"{refused}; measuring the host alone on both sides instead", flush=True)
        # The page list comes from this process's Django, composed with the set, so the run starts again.
        os.execve(
            sys.executable, [sys.executable, "-m", "tools.perf.report"], os.environ | {ALONE_BECAUSE: str(refused)}
        )
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
