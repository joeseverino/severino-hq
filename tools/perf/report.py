"""`mise run perf:report [--image] [--base <ref>]`: how light and fast HQ is.

Boots this checkout's `dev:scratch` seeded with the showcase estate (or, with
`--image`, its built image), signs in as dev_session does, and measures every
page the layout survey walks (`estate.navigation`): the server's time, p50
and p95 over `--requests` requests, and its queries; in Chromium at the phone
and the wide desktop, bytes and requests on a first and a repeat visit, main
thread script time, long tasks and layout shift; memory idle and after one
pass over every page; and the cold start to /health/ready/. It writes the JSON
to var/perf/<commit>.json and prints one table.

`--base <ref>` measures the ref first, its own server in a throwaway worktree
made and removed as `mise run test --at` makes one (tools/gate/at.py), with
this checkout's harness and page list, and prints each number beside how far
it moved. A report, never a gate: it exits 0 whenever it measured, 1 when a
server would not start or answer, 2 for a ref that names no commit.
"""

import json
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

lazy import django
lazy from playwright.sync_api import sync_playwright

from tools.gate.at import RefError, git, remove
from tools.perf import measure, table
from tools.perf.targets import COLD_STARTS, Image, NotMeasured, Scratch, clean_env, run
lazy from hq.platform.core.tests.browser.estate import navigation
lazy from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE, viewport

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var" / "perf"
# The extension set dev:scratch composes, gitignored.
LOCAL = ROOT / "mise.local.toml"


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


def plan() -> Plan:
    """The pages the layout survey walks, the phone's and the wide desktop's viewports, and the task's flags."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    django.setup()
    return Plan(
        pages=navigation(),
        viewports=[viewport(PHONE_SCREEN[0]), viewport(WIDE)],
        requests=int(usage("requests") or 20),
        image=usage("image") == "true",
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
def worktree(ref: str) -> Iterator[tuple[Path, str]]:
    """A detached worktree of ``ref`` outside this checkout, so mise reads only its config, with this
    checkout's mise.local.toml, so its server composes the same extensions; gone however the block ends."""

    commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")[0]
    folder = Path(tempfile.mkdtemp(prefix="hq-perf-base-"))
    tree = folder / "checkout"
    try:
        git("worktree", "add", "--detach", "--quiet", str(tree), commit)
        if LOCAL.is_file():
            shutil.copy2(LOCAL, tree / LOCAL.name)
        yield tree, commit[:12]
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


def measure_target(target: Scratch | Image, asked: Plan) -> dict[str, Any]:
    """Every number for one started target, in the order that keeps each clean: memory before any page."""

    pages = asked.pages
    cookie = target.session()
    idle = target.memory()
    answered = measure.one_pass(target.url, cookie, pages)
    after = target.memory()
    if 200 not in answered.values():
        raise NotMeasured(f"no page answered 200 to the session dev_session minted: {answered}")
    print(f"  server time over {asked.requests} requests a page", flush=True)
    server = measure.server_times(target.url, cookie, pages, asked.requests)
    print("  in Chromium", flush=True)
    with chromium() as launched:
        browser = measure.browser_costs(launched, target.url, cookie, pages, asked.viewports)
    queries = target.queries(pages)
    print(f"  cold start, {COLD_STARTS} times", flush=True)
    starts = [round(target.cold_start(), 1) for _ in range(COLD_STARTS)]
    return {
        "pages": [
            {"page": page, "server": server[page] | {"queries": queries.get(page)}, "browser": browser[page]}
            for page in pages
        ],
        "memory": {"idle_bytes": idle, "after_pass_bytes": after, "measured": target.measured},
        "cold_start": {"runs_ms": starts, "median_ms": sorted(starts)[len(starts) // 2]},
    }


def measure_tree(tree: Path, commit: str, here: bool, asked: Plan) -> dict[str, Any]:
    """One tree's report, written to var/perf/<commit>.json."""

    kind = "image" if asked.image else "dev:scratch"
    log = OUT / f"{commit}.log"
    print(f"Measuring {commit} ({kind}); its server's output is in {log}", flush=True)
    target = Image(image_of(tree, commit, here), log) if asked.image else Scratch(tree, log, trusted=not here)
    with target:
        measured = measure_target(target, asked)
    report = {
        "commit": commit,
        "target": kind,
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "requests": asked.requests,
        "widths": [shown["width"] for shown in asked.viewports],
        **measured,
    }
    (OUT / f"{commit}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def main() -> int:
    signal.signal(signal.SIGTERM, stop)
    OUT.mkdir(parents=True, exist_ok=True)
    ref = usage("base")
    asked = plan()
    try:
        base = None
        if ref:
            with worktree(ref) as (tree, commit):
                base = measure_tree(tree, commit, False, asked)
        here = measure_tree(ROOT, commit_of(ROOT), True, asked)
    except RefError as error:
        print(f"perf:report --base {ref}: {error}", file=sys.stderr)
        return 2
    except NotMeasured as error:
        print(f"perf:report measured nothing: {error}", file=sys.stderr)
        return 1
    print(table.render(here, base))
    print(f"\nWritten to {OUT}/{here['commit']}.json" + (f" and {base['commit']}.json" if base else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
