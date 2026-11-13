"""A page as the browser gates draw it, for a person or an agent to look at.

It runs only as `mise run dev:look`, whose flags say what each does
(`mise run dev:look --help`), and reads what it was asked from that task's
`usage_*` environment, so a run through `manage` without it is refused. How
to use it is in docs/Development.md, under "Browser layout".
"""

import functools
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import override
from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django.test import Client
from django.urls import Resolver404, resolve
lazy from playwright.sync_api import Error as BrowserError

from hq.platform.application import fragments
from hq.platform.core import append_only, seeds
from hq.platform.core.speculation import NOT_A_NAVIGATION
from hq.platform.core.test_runner import stall_watch
from hq.platform.core.tests import task
from hq.platform.core.tests.browser import probes
from hq.platform.core.tests.browser.estate import ExampleEstateBrowserTests, estate_pages, navigation
from hq.platform.core.tests.browser.gate import BrowserGate, add_css
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.audit import AUDIT
from hq.platform.core.tests.browser.reference import (
    REFERENCE,
    REFERENCE_DARK,
    OpenedTag,
    draw_whole_reference,
    open_tag,
    opened_operation,
    opened_walk,
    render_reference,
    served_document,
    wait_for_reference,
)
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.shots import TALLEST, extent_of, shoot_page
from hq.platform.core.tests.browser.stress import ALL_PAGES, stress_pages
from hq.platform.core.tests.browser.survey import palette_probes, survey_checks
from hq.platform.core.tests.browser.widths import PHONE_SCREEN
from hq.platform.core.tests.look_posts import Posted, is_post, post_names, posted
from hq.platform.core.tests.look_records import EstatePages
from hq.platform.core.tests.look_report import (
    Found,
    Measured,
    beside,
    closing,
    looked,
    unmatched,
)
from hq.platform.core.tests.look_tables import COLUMNS, TABLES, columns_line
from hq.platform.core.tests.task import PREFIX, Usage, estate_name, estate_path, is_estate, usage
from hq.platform.core.tests.test_browser_derived_fixtures import derived_pages
lazy from hq.platform.core.example_estate import REQUEST
lazy from hq.platform.core.example_estate.testing import as_production, sign_in

# The catalog builds its entries from the shared parts' constructors as it is
# imported, so a base whose constructors this tree's catalog does not fit still
# looks at every other page: it is read only for a name nothing else is.
lazy from hq.platform.core.tests.browser import catalog

# Each run's shots go to a folder of their own under here, named for when the
# run began, so a later run never overwrites what an earlier one printed: the
# runs from the last `KEPT_FOR` seconds are kept, and at least the newest
# `KEPT_RUNS` whatever their age, and everything else here is removed.
OUT = Path(settings.BASE_DIR) / "var" / "look"
KEPT_RUNS = 5
KEPT_FOR = 24 * 60 * 60
_RUN_NAME = re.compile(r"\d{8}-\d{6}(?:-\d+)?")
# The base's shots, written by the run at the base into this run's folder, so
# the two sides of a change are read together and outlive the base's worktree.
BASE_OUT = "base"
# What a look against a base runs at the base as this checkout has it beside
# this tree's test files, so a finding is new for what the change drew rather
# than for how it was judged: the probes the survey runs.
LENS = (AUDIT.resolve(),)
# A look against a base writes its whole report here and prints only its new
# findings and its verdict, so a long report is never cut short where it is read.
REPORT = Path(settings.BASE_DIR) / "var" / "gate" / "look.txt"
REFERENCE_PAGES = (REFERENCE, REFERENCE_DARK)
# What the console errors, uncaught errors and policy refusals a page had are
# reported as: judged as a rule's findings are, so only a new one fails against a base.
_ERRORS = "the error check"
# A probe's source, as a module of probes/ holds it: a function, its one
# argument a selector or a list of them.
_A_PROBE = re.compile(r"\s*(?:async\s*)?\(")
# A run's verdict when a file changed while it ran.
TREE_CHANGED = "the tree changed while dev:look ran; its shots and verdict mix two trees"

# Request headers the browser sends that are not the view's to read. The
# answer goes back with its type alone, so it is never asked to be compressed.
_TRANSPORT = frozenset({"host", "cookie", "content-length", "connection", "accept-encoding"})
# The header a region asks for its page's part by, as a browser route names it.
_REGION = fragments.HEADER.lower()

# The links a page offers a reader to go to: same origin, a page rather than a
# file, a dialog or a region answered in place, as speculation judges them.
_LINKS = """(notANavigation) => [...document.querySelectorAll('main a[href]')]
  .filter((a) => a.origin === location.origin && !a.matches(notANavigation))
  .map((a) => a.pathname + a.search)"""

# A region waiting on its answer: fragment.js marks one busy until then, and a
# live ask is busy for as long as its job runs.
_BUSY = "[data-fragment][aria-busy=true]:not([data-ask])"
# Every disclosure not yet open, opened, and how many: an exclusive group's
# name is dropped, so opening one closes no other.
_OPEN_ONCE = """(busy) => {
  // A region already waiting is not one this round brings in.
  document.querySelectorAll(busy).forEach((region) => { region.lookWaiting = true; });
  let opened = 0;
  document.querySelectorAll('details:not([open])').forEach((details) => {
    details.removeAttribute('name');
    details.open = true;
    opened += 1;
  });
  return opened;
}"""
# No region a round of opening brought in is waiting on its answer.
_ANSWERED = "(busy) => [...document.querySelectorAll(busy)].every((region) => region.lookWaiting)"
# The rows a reader opens: the folds first, then each row on its own.
_FOLD_TOGGLES = "main button[data-fold-toggle]"
ROW_TOGGLES = "main button[data-row-toggle]:not([data-fold-toggle])"
_PRESS_SHOWN = "(toggles) => document.querySelectorAll(toggles).forEach((t) => t.checkVisibility() && t.click())"
# Rounds of opening, each after what the last one brought in has arrived.
_OPEN_ROUNDS = 10
# The page's extent once it has held still for three polls running: a shot
# and its bands are cut from one reading, so a page still laying itself out
# (a viewer drawing what nears the view) would leave a band outside the image.
# Each shot keeps its own count, by the shot's name.
_EXTENT_HELD = """(shot) => {
  const now = [document.documentElement.scrollWidth, document.documentElement.scrollHeight];
  const held = (window.lookExtent ??= {});
  const last = held[shot] ?? {now: [], runs: 0};
  held[shot] = {now, runs: String(last.now) === String(now) ? last.runs + 1 : 0};
  return held[shot].runs >= 3 && now;
}"""
_EXTENT_POLL = 100

# Every dialog the page declares, by the id after `modal-` (partials/_modal.html).
_DIALOGS = """() => [...document.querySelectorAll('dialog.modal[id^="modal-"]')].map((dialog) => dialog.id.slice(6))"""
# Which of a dialog's openers a reader can press, as its place among them: one
# shown, or one inside closed menus they open first, by their summaries. The
# menus opened are kept to close once the dialog is done; -1 for none.
_REACH = """(openers) => {
  window.lookReached = [];
  const all = [...document.querySelectorAll(openers)];
  const shown = all.findIndex((opener) => opener.checkVisibility());
  if (shown >= 0) return shown;
  for (const [place, opener] of all.entries()) {
    const closed = [];
    for (let menu = opener.closest('details:not([open])'); menu; menu = menu.parentElement.closest('details:not([open])')) {
      closed.unshift(menu);
    }
    const opened = [];
    for (const menu of closed) {
      const summary = menu.querySelector(':scope > summary');
      if (!summary?.checkVisibility()) break;
      summary.click();
      opened.push(summary);
    }
    if (opened.length && opener.checkVisibility()) {
      window.lookReached = opened;
      return place;
    }
    opened.reverse().forEach((summary) => summary.click());
  }
  return -1;
}"""
_LEAVE = "() => window.lookReached.reverse().forEach((summary) => summary.closest('details').open && summary.click())"
# The dialog open, and no region in it waiting on its answer.
_DIALOG_READY = """([id, busy]) => {
  const dialog = document.getElementById(`modal-${id}`);
  return dialog.open && !dialog.querySelector(busy);
}"""
# Where the window is scrolled, and the window put back there.
_WINDOW_AT = "() => [window.scrollX, window.scrollY]"
_WINDOW_TO = "([x, y]) => window.scrollTo(x, y)"
_DIALOG_CLOSED = "(id) => !document.getElementById(`modal-${id}`).open"
# How far a dialog scrolls within itself, and a place to scroll it to.
_DIALOG_EXTENT = (
    "(id) => { const d = document.getElementById(`modal-${id}`); return [d.scrollHeight, d.clientHeight]; }"
)
_DIALOG_SCROLL = "([id, top]) => { document.getElementById(`modal-${id}`).scrollTop = top; }"

# The task's one required argument: mise exports it on every run of the task,
# so a process without it was not started by the task.
_ASKED = "pages"
# Where a look at the base writes what it found, for the run that asked for it:
# a usage variable of the task's no person sets, given only by that run.
_FINDINGS = "found_into"
# The instant the run that asked began, in seconds, handed to its base the
# same way: both draw with the clock frozen there.
_CLOCK = "clock_at"
# The folder a look at the base shoots into, handed to it the same way.
_SHOTS = "shots_into"


def _flush() -> None:
    with append_only.lifted():
        call_command("flush", interactive=False, verbosity=0)


def _asked() -> Usage:
    """What the task was given. Without the task's environment every flag would read as unset."""

    given = usage()
    if not given.given(_ASKED):
        raise RuntimeError(
            f"dev:look reads what it was asked from its task's environment, and {PREFIX}{_ASKED} is not set:"
            " run it as `mise run dev:look <pages>...`, never as a test label through manage"
        )
    return given


def _listed(name: str) -> list[str]:
    """A flag that takes a comma-separated list."""

    return [part.strip() for part in _asked().text(name).split(",") if part.strip()]


def _css(given: str) -> str:
    """``--css`` as CSS text: the text itself, or the file it names, a
    relative path read from the checkout. Text holds a rule's braces; a path
    never does."""

    if not given or "{" in given:
        return given
    path = Path(settings.BASE_DIR, given)
    if not path.is_file():
        raise AssertionError(f"--css {given!r} is no CSS (no rule in it) and no file ({path})")
    return path.read_text(encoding="utf-8")


def _script(given: str) -> str:
    """``--eval`` as script text: the file it names, a relative path read
    from the checkout."""

    if not given:
        return ""
    path = Path(settings.BASE_DIR, given)
    if not path.is_file():
        raise AssertionError(f"--eval {given!r} names no file ({path})")
    return path.read_text(encoding="utf-8")


def _probe(given: str) -> tuple[str, str]:
    """``--probe`` as its name and the source it names, ``<module>:<NAME>`` of
    a probe module (``boxes:ASIDES_WRAPPED_WITH_ROOM``); a name that is none
    is refused with those its module offers."""

    if not given:
        return "", ""
    module, _, name = given.partition(":")
    folder = Path(probes.__file__).parent
    modules = sorted(path.stem for path in folder.glob("*.py") if not path.stem.startswith("_"))
    if module not in modules or not name:
        raise AssertionError(f"--probe takes <module>:<NAME>, a module of {_shown(folder)}/: {', '.join(modules)}")
    held = vars(importlib.import_module(f"{probes.__name__}.{module}"))
    offered = sorted(
        key for key, value in held.items() if key.isupper() and isinstance(value, str) and _A_PROBE.match(value)
    )
    if name not in offered:
        raise AssertionError(f"--probe {given!r} is no probe of {module}; it offers {', '.join(offered)}")
    return name, held[name]


def _argument(given: str):
    """``--arg`` as the value it writes in JSON; none is null."""

    if not given:
        return None
    try:
        return json.loads(given)
    except json.JSONDecodeError as broken:
        raise AssertionError(f"--arg {given!r} is no JSON ({broken}); a selector is a quoted string") from None


def _probed(where: str, width: int, probe: str, outcome) -> str:
    """What ``--probe`` found on one shot: each finding on a line of its own
    under the page, the width and the probe, anything else it returns as
    ``--eval``'s value reads, or what it threw."""

    returned, value = outcome
    head = f"{where} at {width}, {probe}"
    if not returned:
        return f"{head} threw ({(str(value).splitlines() or [''])[0]})"
    if isinstance(value, list) and all(isinstance(line, str) for line in value):
        said = [f"{head}: {len(value) or 'nothing'} found", *value]
    else:
        said = [f"{head} returned:", *json.dumps(value, indent=2, ensure_ascii=False).splitlines()]
    return "\n".join([said[0], *(f"  {line}" for line in said[1:])])


def _evaluated(page: str, width: int, value) -> str:
    """What ``--eval`` returned on one page at one width: text as it is,
    anything else as indented JSON, under the page and width."""

    said = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    return f"{page} at {width}:\n" + "\n".join(f"  {line}" for line in said.splitlines() or [""])


def _bands(height: int, band: int) -> list[tuple[int, int]]:
    """Where a page ``height`` tall is cut into bands ``band`` tall, as each
    band's top and height, top to bottom: none for a page one band holds."""

    if height <= band:
        return []
    return [(top, min(band, height - top)) for top in range(0, height, band)]


# A shot file by its name, and the part of the page it holds, as its top and height.
type Band = tuple[str, float, float]

# What stands for an element ``--eval``'s script returned, by its place among
# them, until its box is read once the shot is taken.
_ELEMENT = "dev:look element"
# The script's value with each element in it, at any depth of arrays and
# plain objects, kept in the page and stood for by its place.
_RETURNED = """([value, marker]) => {
  const returned = (window.lookReturned = []);
  const made = new Map();
  const walk = (item) => {
    if (item instanceof Element) return {[marker]: returned.push(item) - 1};
    if (item instanceof NodeList || item instanceof HTMLCollection) item = [...item];
    if (item === null || typeof item !== 'object') return item;
    if (made.has(item)) return made.get(item);
    if (Array.isArray(item)) {
      const out = [];
      made.set(item, out);
      item.forEach((each) => out.push(walk(each)));
      return out;
    }
    if (Object.getPrototypeOf(item) !== Object.prototype) return item;
    const out = {};
    made.set(item, out);
    Object.entries(item).forEach(([key, each]) => { out[key] = walk(each); });
    return out;
  };
  return walk(value);
}"""
# Each element kept, named, with its box from the top of what ``frame``
# scrolls, or the page's without one, and whether a reader sees it at all.
_RETURNED_BOXES = """(frame) => {
  const scroller = frame && document.querySelector(frame);
  const origin = scroller
    ? scroller.getBoundingClientRect().top + scroller.clientTop - scroller.scrollTop
    : -window.scrollY;
  return (window.lookReturned ?? []).map((el) => {
    const box = el.getBoundingClientRect();
    const classes = [...el.classList].slice(0, 2).map((name) => `.${name}`).join('');
    return {
      named: `${el.localName}${el.id ? `#${el.id}` : ''}${classes}`,
      top: box.top - origin,
      bottom: box.bottom - origin,
      shown: el.checkVisibility(),
    };
  });
}"""
# The box of the operation whose id in the viewer ends with ``suffix``
# (`/<METHOD><path>`), from the top of the page, as the returned elements'.
_OPERATION_BOX = """([root, suffix]) => [...document.querySelector(root).querySelectorAll('[id*="/tag/"]')]
  .filter((el) => el.id.endsWith(suffix)).slice(0, 1).map((el) => {
    const box = el.getBoundingClientRect();
    return {top: box.top + window.scrollY, bottom: box.bottom + window.scrollY, shown: el.checkVisibility()};
  })"""
# The part of the page the window shows, as its top and height.
_WINDOW_SHOWS = "() => [window.scrollY, window.innerHeight]"


def _shown_in(box: dict, bands: list[Band]) -> str:
    """Which of the shot files ``bands`` names show ``box``: the one, or the
    first and last of those it spans."""

    if not box["shown"]:
        return "not shown"
    top, bottom = box["top"], max(box["bottom"], box["top"] + 1)
    files = [file for file, at, tall in bands if top < at + tall and bottom > at]
    if not files:
        return "in no shot, outside what they hold"
    return f"in {files[0]}" + (f" to {files[-1]}" if len(files) > 1 else "")


def _placed(value, boxes: Callable[[], list[dict]], bands: list[Band]):
    """``value`` with each element the script returned said as what it is
    and the shot file that shows it, ``boxes`` read from the page only when
    there is one."""

    if isinstance(value, dict):
        if set(value) == {_ELEMENT}:
            box = boxes()[value[_ELEMENT]]
            return f"{box['named']}, {_shown_in(box, bands)}"
        return {key: _placed(item, boxes, bands) for key, item in value.items()}
    if isinstance(value, list):
        return [_placed(item, boxes, bands) for item in value]
    return value


def _write_found(path: Path, found: Found, measured: Measured, errors: Found) -> None:
    entries = [
        {
            "page": name,
            "width": width,
            "found": said,
            "measured": measured.get((name, width), {}),
            "errors": errors.get((name, width), []),
        }
        for (name, width), said in found.items()
    ]
    path.write_text(json.dumps(entries), encoding="utf-8")


def _read_found(path: Path) -> tuple[Found, Measured, Found]:
    entries = json.loads(path.read_text(encoding="utf-8"))

    def each(field: str) -> dict:
        return {(entry["page"], entry["width"]): entry[field] for entry in entries}

    return each("found"), each("measured"), each("errors")


def _commit(ref: str) -> str:
    """The commit ``ref`` names in this checkout, and for HEAD whether the
    checkout holds changes past it: what a report was looked at."""

    git = ["git", "-C", str(settings.BASE_DIR)]
    sha = subprocess.run([*git, "rev-parse", "--short", ref], capture_output=True, text=True, check=False).stdout
    said = sha.strip() or f"{ref} (unknown)"
    if ref == "HEAD":
        changed = subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True, check=False).stdout
        said += " with uncommitted changes" if changed.strip() else ""
    return said


def tree_of(root: Path) -> str:
    """The checkout at ``root`` as one git tree id: every file in it but those
    git ignores, untracked ones too, so two checkouts that differ in any file
    name different trees. It is written from an index and an object store of
    its own, the checkout's objects read beside them, so the checkout's index
    and objects are left as they were."""

    objects = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-path", "objects"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    with tempfile.TemporaryDirectory() as folder:
        Path(folder, "objects").mkdir()
        env = os.environ | {
            "GIT_INDEX_FILE": str(Path(folder, "index")),
            "GIT_OBJECT_DIRECTORY": str(Path(folder, "objects")),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects,
        }
        subprocess.run(["git", "-C", str(root), "add", "--all"], env=env, capture_output=True, check=True)
        written = subprocess.run(
            ["git", "-C", str(root), "write-tree"], env=env, capture_output=True, text=True, check=True
        )
        return written.stdout.strip()


def changed_since(tree: str, root: Path) -> str:
    """The run's refusal when the checkout at ``root`` is no longer ``tree``,
    the one it began on: its pages were drawn from both."""

    return "" if tree_of(root) == tree else TREE_CHANGED


def _relay(command: list[str], sink, ref: str, **options) -> None:
    """Run ``command``, each line it writes kept in ``sink`` and said here as
    it arrives, after ``ref``, so the base's pass reads apart from this one's
    and a hang there names its page. The command is killed if this run ends
    first."""

    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, **options
    ) as run:
        try:
            for line in run.stdout or ():
                sink.write(line)
                print(f"{ref}: {line}", end="", flush=True)
        except BaseException:
            run.kill()
            raise


def _look_at(ref: str, rule: str, clock_at: float) -> tuple[Found, Measured, Found]:
    """What this run finds, measures and hears go wrong, looked for at ``ref``: the same
    task's run, in a worktree of it with this checkout's test files and lens
    (`LENS`) laid over it as `test --at` lays them, so a base older than a test
    file still looks and both are judged alike. Its environment is this run's, but for a
    database of its own, this run's clock, and the CSS already read and the
    script's absolute path, since a relative path names this checkout, and
    without ``--css``'s sheet."""

    checkout = Path(settings.BASE_DIR).resolve()
    try:
        commit = task.commit_of(ref, checkout)
        present, deleted = task.changed_tests(commit, checkout)
    except task.RefError as error:
        raise AssertionError(f"--base {ref}: {error}") from None
    with task.worktree(commit, checkout, lint_tools=True) as tree:
        lens = [str(path.relative_to(checkout)) for path in LENS]
        task.overlay(tree, [*present, *lens], deleted, checkout)
        found, log = tree.parent / "found.json", tree.parent / "look.log"
        # The kept test database is migrated for this checkout's code, so the base makes its own in its worktree.
        left = {f"{PREFIX}base", "VIRTUAL_ENV", "SEVERINO_TEST_DATABASE_PATH"}
        env = {name: value for name, value in os.environ.items() if name not in left}
        env |= {
            PREFIX + _FINDINGS: str(found),
            PREFIX + _CLOCK: str(clock_at),
            "SEVERINO_DATABASE_PATH": str(tree.parent / "look.sqlite3"),
            f"{PREFIX}rule": rule,
            # Drawn as its tree draws, so a sheet laid over this side reads as what it fixes and breaks.
            f"{PREFIX}css": "",
            PREFIX + _SHOTS: str(this_run() / BASE_OUT),
        }
        if script := _asked().text("eval"):
            env[f"{PREFIX}eval"] = str(Path(settings.BASE_DIR, script).resolve())
        print(f"Looking at {ref} first, in {tree}", flush=True)
        # The run there is watched by its own stall watch.
        with log.open("w", encoding="utf-8") as sink, stall_watch.paused():
            # The command this run was started with, run there by its own environment.
            _relay(["uv", "run", "--group", "browser", "python", *sys.argv], sink, ref, cwd=tree, env=env)
        if not found.is_file():
            said = log.read_text(encoding="utf-8").splitlines()[-40:]
            raise AssertionError(f"The look at {ref} did not finish:\n" + "\n".join(said))
        return _read_found(found)


def _address(url: str) -> str:
    """A URL's path and query: where it is on whichever origin."""

    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _guessable(path: str) -> bool:
    """Whether a browser could request ``path`` speculatively: the rules
    prefetch only a route behind the session (`speculation.sessionless`)."""

    try:
        view = resolve(path).func
    except Resolver404:
        return True
    return getattr(view, "login_required", True)


@functools.cache
def this_run() -> Path:
    """The folder this run shoots into: the one its asker handed a look at a
    base, or else a new one under ``OUT``."""

    if given := _asked().text(_SHOTS):
        folder = Path(given)
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    return _new_run(OUT)


def _new_run(root: Path, kept: int = KEPT_RUNS) -> Path:
    """A new folder in ``root`` named for now, kept with the runs from the last
    ``KEPT_FOR`` seconds and the newest ``kept`` in all, and every other entry
    of ``root`` removed. A run's age is read from its name."""

    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    while True:
        runs = sorted(
            (entry for entry in root.iterdir() if _RUN_NAME.fullmatch(entry.name) and entry.is_dir()),
            key=lambda entry: [int(part) for part in entry.name.split("-")],
        )
        # Past the newest run of this second, so a name pruned this second is never used again.
        newest = runs[-1].name if runs else ""
        again = int(newest.removeprefix(stamp).lstrip("-") or 0) + 1 if newest.startswith(stamp) else 0
        folder = root / (f"{stamp}-{again}" if again else stamp)
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        break
    cutoff = time.time() - KEPT_FOR
    recent = [run for run in runs if time.mktime(time.strptime(run.name[:15], "%Y%m%d-%H%M%S")) >= cutoff]
    keep = {folder, *recent, *(runs[-(kept - 1) :] if kept > 1 else ())}
    # Another run pruning at once may take an entry first.
    for entry in root.iterdir():
        if entry in keep:
            continue
        if entry.is_dir() and not entry.is_symlink():
            with suppress(FileNotFoundError):
                shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)
    return folder


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")


def _shot(name: str, width: int, part: str = "") -> Path:
    """Where the shot of ``name`` at ``width`` goes, or of a ``part`` of it
    open, such as ``dialog-<id>`` or ``popover-<id>``."""

    shot = this_run() / f"{_slug(name)}-{width}.png"
    return shot.with_name(f"{shot.stem}.{_slug(part)}.png") if part else shot


def _with(shows: str, bands: int) -> str:
    """What a shot shows, and the bands cut from it beside it."""

    return ", ".join(filter(None, [shows, f"{bands} bands beside it (.band-NN.png)" if bands else ""]))


def _shown(path: Path) -> str:
    """A shot's path as a reader in the checkout names it."""

    return str(path.relative_to(settings.BASE_DIR)) if path.is_relative_to(settings.BASE_DIR) else str(path)


def _rules() -> list[str]:
    """What ``--rule`` may name: each check that takes a page's name alone."""

    return sorted(
        name
        for owner in (Look, LayoutBrowserTests)
        for name, member in vars(owner).items()
        if inspect.isfunction(member) and list(inspect.signature(member).parameters)[1:] in (["name"], ["_name"])
    )


def no_rule(rule: str) -> str:
    """Why ``--rule`` refuses ``rule``: a check of the survey is no rule of its
    own, so the refusal names the gate test that holds it."""

    said = f"No rule named {rule!r}."
    if rule in survey_checks("/", PHONE_SCREEN[0]):
        held = "every_palette_check_holds" if rule in palette_probes() else "every_layout_check_holds"
        said += f" {rule!r} is a check of the survey, held by {held}: --rule {held}, or --rule survey for every check."
    return f"{said} Rules: {', '.join(_rules())}"


def _families(asked: tuple[str, ...]) -> list[str]:
    """``asked`` with each name that begins catalog entries rather than naming
    one (``part/notice``, ``table``) spelled out as those entries, in the
    catalog's order."""

    spelled = []
    for name in asked:
        family = [entry for entry in catalog.CATALOG if entry.startswith((f"{name}/", f"{name}-"))]
        spelled += family if family and name not in catalog.CATALOG else [name]
    return spelled


def _named() -> list[str]:
    """Every page dev:look knows by name but a catalog entry."""

    return [*ALL_PAGES, *derived_pages(), *REFERENCE_PAGES]


def catalog_pages(asked) -> dict[str, str]:
    """The catalog entries ``asked`` names, each drawn as the catalog's own
    gate draws it, so a look and that gate never draw an entry apart. An
    entry is the one name ``_pages`` hands on that is no other kind."""

    known = {*_named(), *post_names()}
    entries = {name for name in asked if not is_estate(name) and name not in known}
    if not entries:
        return {}
    return {name: page for name, page in catalog.CatalogLayoutTests.render().items() if name in entries}


def _pages(asked: tuple[str, ...]) -> list[str]:
    """The pages ``asked`` names, ``all`` and ``estate/all`` spelled out, an
    estate page by its name; one dev:look cannot draw is refused. The catalog
    is read only for ``all`` or a name no other page is, so its refusal
    offers its entries too."""

    named, posts = _named(), post_names()
    try:
        with EstatePages(_asked().text("seed")) as records:
            found = None
            if asked and "all" not in asked:
                with suppress(task.PageRefused):
                    found = task.pages(asked, estate_pages(), [*named, *posts], records)
            if found is None:
                named += catalog.CATALOG
                found = task.pages(_families(asked), estate_pages(), [*named, *posts], records)
    except task.PageRefused as refused:
        raise AssertionError(str(refused)) from None
    return [page if page in named or page in posts else estate_name(page) for page in found]


class Look(LayoutBrowserTests, ExampleEstateBrowserTests):
    """The layout gate's rules and pages, with the example estate's way of
    loading a page whose parts arrive after it."""

    # Whether the page open is the API reference with its viewer drawn, as
    # ``open`` finds it.
    on_reference = False
    # Whether the run takes its screenshots, as ``asked_for`` decides.
    shooting = True
    # A look's record is its findings and shots; a trace would snapshot the
    # page at every step of every page the look reads.
    traced = False

    # What the run was asked and the base it is held to, set once by `setUpClass`.
    tree: str
    clock_at: float
    asked: list[str]
    base: Found | None
    base_measured: Measured
    base_errors: Found
    # The probe --probe names, its source, and the argument --arg gives it.
    probe = probe_source = ""
    probe_arg: object = None
    # Each estate page asked for by its address, the pages followed added.
    addresses: dict[str, str]
    estate_client: Client | None
    # Each estate address answered, with its status and any speculation refusal.
    answered: dict[str, tuple[int, str]]

    @classmethod
    @override
    def setUpClass(cls):
        # The tree the run draws from, held to the end (`changed_since`).
        cls.tree = tree_of(Path(settings.BASE_DIR))
        cls.clock_at = float(_asked().text(_CLOCK) or round(time.time()))
        # What was asked is checked before the base is looked at, which takes a
        # while: the seed first, as a record's page is found in its estate.
        try:
            seeds.get(_asked().text("seed"))
        except LookupError as unknown:
            raise AssertionError(f"--seed: {unknown}") from None
        cls.asked = _pages(_asked().repeated("pages"))
        if (rule := cls.rule_asked()) and rule not in _rules():
            raise AssertionError(no_rule(rule))
        cls.probe, cls.probe_source = _probe(_asked().text("probe"))
        cls.probe_arg = _argument(_asked().text("arg"))
        if _asked().text("arg") and not cls.probe:
            raise AssertionError("--arg is what --probe's function takes: name the probe")
        if _asked().text("operation") and not set(cls.asked) & set(REFERENCE_PAGES):
            raise AssertionError(
                f"--operation opens an operation of the API reference: name {' or '.join(REFERENCE_PAGES)}"
            )
        # The base next, while nothing of this run's holds memory but Django.
        ref = _asked().text("base")
        cls.base, cls.base_measured, cls.base_errors = _look_at(ref, rule, cls.clock_at) if ref else (None, {}, {})
        super().setUpClass()

    @override
    def start(self, **options):
        super().start(**options)
        # Date reads the same instant all run, timers still run.
        self.page.clock.set_fixed_time(self.clock_at)

    @classmethod
    def rule_asked(cls):
        return _asked().text("rule") or ("survey" if _asked().text("base") else "")

    @classmethod
    @override
    def render(cls):
        pages = cls.asked
        estate = [estate_name(path) for path in navigation()]
        cls.addresses = {name: estate_path(name) for name in {*estate, *filter(is_estate, pages)}}
        cls.addresses |= {name: posted(name).address() for name in filter(is_post, pages)}
        rendered = stress_pages(pages) | catalog_pages(pages)
        if set(pages) & set(REFERENCE_PAGES):
            rendered |= render_reference()
        # Last: the stress estates are rolled back, the example estate is
        # committed, and both sign in as the same owner. A dialog fills itself
        # from its region's own request, which only a server answers, so a
        # dialog asked for on a page answered from memory is served too.
        served = any(is_estate(name) or is_post(name) for name in pages) or bool(_listed("dialog"))
        cls.estate_client = cls.serve_estate() if served else None
        return rendered

    @classmethod
    def serve_estate(cls):
        """A client signed in as the example estate's owner, answering as
        production does, until the class is done.

        The estate is committed to the run's own test database, which the
        runner destroys after it: `respond` runs in Playwright's event loop,
        where Django opens a connection of its own, and that one sees only
        what is committed.
        """

        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        # Last out, once the browser is gone: the committed estate, as Django
        # empties the database after a test that commits, so a class run after
        # this one builds its estates into the database the runner made. The
        # audit log's guard refuses that flush, so it is lifted for it.
        stack.callback(_flush)
        # Django takes Playwright's event loop for async code sharing a
        # connection. It is this one thread, and nothing else runs while a
        # request is answered.
        stack.enter_context(mock.patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "1"}))
        stack.enter_context(as_production())
        client = Client()
        sign_in(client, stack.enter_context(seeds.built(_asked().text("seed"))))
        cls.answered = {}
        return client

    # Whether the page in the browser is the example estate's. A stress page
    # and an estate page can share an address (`projects`), so the address
    # alone cannot say whose it is.
    on_estate = False
    # The named POST the next load of its address sends, set by ``open``.
    sending: Posted | None = None

    @override
    def answer(self, route):
        """A stress page or a static file, as the layout gate answers it:
        `respond` answers the estate's pages itself."""

        BrowserGate.answer(self, route)

    @override
    def respond(self, route):
        request = route.request
        parts = urlsplit(request.url)
        # A region of another page asks the server for that page's region,
        # where a page in memory would answer it whole.
        another_region = _REGION in request.headers and parts.path != urlsplit(self.page.url).path
        if (
            self.estate_client is None
            or parts.path.startswith(settings.STATIC_URL)
            or (not self.on_estate and parts.path.strip("/") in self.pages and not another_region)
        ):
            LayoutBrowserTests.respond(self, route)
            return
        if request.method != "GET":
            route.fulfill(status=405, body="dev:look only reads")
            return
        address = _address(request.url)
        headers = {
            f"HTTP_{name.upper().replace('-', '_')}": value
            for name, value in request.headers.items()
            if name not in _TRANSPORT
        }
        # A browser route cannot answer with a redirect, so it is followed here.
        sending = self.sending
        if sending is not None and request.is_navigation_request() and address == sending.address():
            # The page a named POST lands on: the form sent once, as its page sends it.
            self.sending = None
            response = sending.send(self.estate_client, **headers)
        else:
            # Marked as a guess, so a page that would act or reach out is refused,
            # except where no browser guesses: a route that answers without a
            # session is never prefetched, and a guess there is refused whole, so
            # the page's own fetch of one (the reference's document) is sent as made.
            if _guessable(parts.path):
                headers["HTTP_SEC_PURPOSE"] = "prefetch"
            response = self.estate_client.get(address, secure=True, follow=True, **{**headers, **REQUEST})
        self.answered[address] = (response.status_code, response.get("X-Speculation-Refused", ""))
        body = response.getvalue()
        # The event loop's own connection, closed rather than left to leak.
        connections.close_all()
        route.fulfill(
            status=response.status_code,
            headers={"content-type": response.get("Content-Type", "text/html")},
            body=body,
        )

    @override
    def visit(self, path, width):
        self.on_estate = True
        super().visit(path, width)

    @override
    def open(self, name, width):
        if is_estate(name):
            self.visit(self.addresses[name], width)
        elif is_post(name):
            self.sending = posted(name)
            self.visit(self.addresses[name], width)
        else:
            self.on_estate = False
            super().open(name, width)
        # The viewer mounts on its root wherever a page carries one: the
        # reference HQ serves and the gate's copy in the operator's dark theme.
        reference = self.page.locator(SELECTORS["reference"]).count() > 0
        self.on_reference = reference and not _asked().switch("no_script")
        if self.on_reference:
            wait_for_reference(self.page)
            draw_whole_reference(self.page)
        # On every load, so each state shot again from a fresh page (a tag of
        # the reference opened) draws under it too.
        if self.css:
            add_css(self.page, self.css)
            self.settle()

    def test_look(self):
        widths, rule = self.asked_for()
        self.start(java_script_enabled=not _asked().switch("no_script"))
        if _asked().switch("dark"):
            self.page.emulate_media(color_scheme="dark")
        self.errors: Found = {}
        self.unanswered_pages: dict[str, int | None] = {}
        if _asked().switch("deep"):
            self.follow(widths[0])
        self.measured: Measured = {}
        found = self.look_at_every_page(widths, rule)
        if into := _asked().text(_FINDINGS):
            _write_found(Path(into), found, self.measured, self.errors)
        if failed := self.close(rule, found):
            self.fail("\n".join(failed))

    def close(self, rule, found) -> list[str]:
        """What the run found and heard said, each new one against a base on
        a line and each verdict last, the rule's report kept in ``REPORT``;
        the verdicts that failed."""

        ref = _asked().text("base")
        base_errors = self.base_errors if self.base is not None else None
        report, printed, errors_failed = closing(_ERRORS, self.errors, base_errors, ref)
        failed = [printed[-1]] if errors_failed else []
        if rule:
            judged, said, rule_failed = closing(rule, found, self.base, ref)
            head = _commit("HEAD") + (f" against {ref} at {_commit(ref)}" if self.base is not None else "")
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(f"{head}\n{judged}\n{report}\n", encoding="utf-8")
            printed = [f"Page by page: {REPORT}", *said, *printed]
            failed = [said[-1]] * rule_failed + failed
        # A page that did not answer was not looked at, here or at a base, so it fails either way.
        unanswered = [
            f"{name} answered {status}, not {posted(name).status if is_post(name) else 200}"
            for name, status in self.unanswered_pages.items()
        ]
        base = f", the base's in {_shown(this_run() / BASE_OUT)}/" if self.base is not None else ""
        shots = f"Screenshots in {_shown(this_run())}/{base}"
        if not self.shooting:
            shots = f"Findings in {_shown(this_run())}/, no screenshots: --shots takes them"
        # Last, as it voids every verdict before it.
        changed = [moved] if (moved := changed_since(self.tree, Path(settings.BASE_DIR))) else []
        print(*printed, *unanswered, shots, *changed, sep="\n", flush=True)
        return failed + unanswered + changed

    def look_at_every_page(self, widths, rule) -> Found:
        """Each page at each width, said as it starts, so a look that hangs
        names where, and once it has been looked at: its shots, its findings
        (kept beside its screenshot) and every error and refusal it had."""

        found: Found = {}
        for name in self.asked:
            for width in widths:
                stall_watch.mark(f"dev:look {name} at {width}")
                print(f"Looking at {name} at {width}", flush=True)
                self.shots: list[tuple[str, str]] = []
                said = found[name, width] = self.look(name, width, rule)
                errors = self.errors[name, width] = self.refusals_so_far()
                report = _shot(name, width).with_suffix(".txt")
                if said:
                    report.write_text("\n".join([f"{name} at {width}", *said]) + "\n", encoding="utf-8")
                else:
                    report.unlink(missing_ok=True)
                print(*looked(self.shots, said, rule, _shown(report), errors), sep="\n", flush=True)
        return found

    def follow(self, width):
        """Every page the estate pages asked for link to, one per address."""

        seen = {urlsplit(self.addresses[name]).path for name in self.asked if is_estate(name)}
        for name in [name for name in self.asked if is_estate(name)]:
            stall_watch.mark(f"dev:look following the links of {name}")
            self.visit(self.addresses[name], width)
            for address in self.page.evaluate(_LINKS, NOT_A_NAVIGATION):
                path = urlsplit(address).path
                if path in seen or path.startswith(settings.STATIC_URL):
                    continue
                seen.add(path)
                followed = estate_name(address)
                self.addresses[followed] = address
                self.asked.append(followed)
        # Each page followed from is looked at again at this width, and says them then.
        self.refusals_so_far()

    def asked_for(self):
        """The widths and rule the task was given, and the CSS it adds."""

        widths = [int(width) for width in _listed("widths")] or [PHONE_SCREEN[0], 820, 1360]
        rule = self.rule_asked()
        self.css = _css(_asked().text("css"))
        self.script = _script(_asked().text("eval"))
        # A run that judges is read by its findings, and a shot and its bands
        # cost more than the rule that reads the page, so it shoots only when
        # asked or when what it was asked reads the shots: a base's are kept
        # beside this run's, and --eval and --operation name the band that
        # shows what they point at.
        asked = _asked()
        self.shooting = not rule or asked.switch("shots") or any(map(asked.text, ("base", "eval", "operation", _SHOTS)))
        self.dialogs = _listed("dialog")
        unnamed = [dialog for dialog in self.dialogs if not re.fullmatch(r"[\w-]+", dialog)]
        self.assertFalse(unnamed, f"--dialog takes dialog ids (connection) or all, not {unnamed}")
        return widths, rule

    def look(self, name, width, rule) -> list[str]:
        """Screenshot ``name`` at ``width``, and what ``rule`` finds there."""

        if refused := self.refused(name, width):
            return self.shoot_failure(name, width, refused)
        self.draw_as_asked(name, width)
        opened = self.open_operation(name, width) if self.on_reference and _asked().text("operation") else None
        shows = f"with {opened.label} opened" if opened else ""
        # After --open, --expand and --operation, so it reads the page the shot shows.
        bands = self.take_shot(name, width, lambda: self.screenshot(name, width, shows=shows), shows=shows)
        if opened:
            self.say_where(opened, bands)
        if _asked().switch("columns"):
            self.measure_columns(name, width)
        self.width = width
        said = self.judged(name, width, rule) if rule else []
        if rule and _asked().switch("open"):
            said += self.with_each_popover(name, width, rule, said)
        said += self.with_each_dialog(name, width, rule, said)
        if self.on_reference and _asked().switch("open"):
            said += self.with_each_tag_opened(name, width, rule, said)
        return said

    def judged(self, name, width, rule) -> list[str]:
        """What ``rule`` finds on the page as it stands."""

        try:
            getattr(self, rule)(name)
        except AssertionError as found:
            return [line.strip() for line in str(found).splitlines() if line.strip()]
        except BrowserError as broke:
            return self.shoot_failure(name, width, f"{rule} could not run ({str(broke).splitlines()[0]})")
        return []

    def with_each_popover(self, name, width, rule, beneath) -> list[str]:
        """What ``rule`` finds with one popover of each kind shown
        (``each_popover_kind``), past what it finds on the page ``beneath``."""

        found = []
        for (label, slug), alike in self.each_popover_kind():
            self.settle()
            named = f"{label} and {alike} like it" if alike else label
            shows = f"with {named} shown"
            self.take_shot(
                name, width, functools.partial(self.shoot_popover, name, width, slug, shows), f"popover-{slug}", shows
            )
            said = self.judged(name, width, rule)
            # A finding names what was shown and leaves the path to the shot
            # line beside it, since a base's shots lie elsewhere and a finding
            # is matched with the base's by its words.
            found += [f"with {named} shown: {line}" for line in unmatched(said, beneath)]
        return found

    def with_each_tag_opened(self, name, width, rule, beneath) -> list[str]:
        """The API reference with each of its tags opened in turn, on a page
        of its own (``<page>-<width>.open-<tag>.png``), and what ``rule``
        finds there past what it finds on the page ``beneath``. Its viewer
        opens with buttons rather than ``details``, and the whole of it opened
        is more page than a browser shoots, so a tag is opened at a time, each
        schema where the walk first meets it (``opened_walk``)."""

        found = []
        for opened in opened_walk(served_document(self.page)):
            stall_watch.mark(f"dev:look {name} at {width} with {opened.label} opened")
            self.open(name, width)
            undrawn = open_tag(self.page, opened)
            self.settle()
            shows = f"with {opened.label} opened"
            part = f"open-{opened.name}"
            self.take_shot(name, width, functools.partial(self.screenshot, name, width, part, shows), part, shows)
            said = [f"the viewer drew nothing for {', '.join(undrawn)}"] if undrawn else []
            said += self.judged(name, width, rule) if rule else []
            found += [f"{shows}: {line}" for line in unmatched(said, beneath)]
        return found

    def shoot_popover(self, name, width, popover, shows) -> list[Band]:
        """The window with ``popover`` shown (``<page>-<width>.popover-<id>.png``),
        where it sits, which ``shows`` says; the part of the page it holds."""

        shot = _shot(name, width, f"popover-{popover}")
        self.page.screenshot(path=shot, animations="disabled")
        self.shots.append((_shown(shot), shows))
        top, tall = self.page.evaluate(_WINDOW_SHOWS)
        return [(shot.name, top, tall)]

    def with_each_dialog(self, name, width, rule, beneath) -> list[str]:
        """Each dialog ``--dialog`` names, or every one the page declares for
        ``all``, opened as a reader opens it and shot
        (``<page>-<width>.dialog-<id>.png``), with ``--eval`` run and what
        ``rule`` finds past what it finds on the page ``beneath``."""

        asked = self.dialogs
        if not asked:
            return []
        declared = self.page.evaluate(_DIALOGS)
        found = []
        for dialog in declared if asked == ["all"] else asked:
            if dialog not in declared:
                print(
                    f"{name} at {width}: no dialog {dialog!r}; it declares {', '.join(declared) or 'none'}", flush=True
                )
                continue
            stall_watch.mark(f"dev:look {name} at {width} with #modal-{dialog} open")
            if not self.open_dialog(name, width, dialog):
                continue
            self.take_shot(
                name,
                width,
                functools.partial(self.shoot_dialog, name, width, dialog),
                f"dialog-{dialog}",
                f"with #modal-{dialog} open",
                frame=f"#modal-{dialog}",
            )
            if rule:
                said = self.judged(name, width, rule)
                found += [f"with #modal-{dialog} open: {line}" for line in unmatched(said, beneath)]
            self.close_dialog(dialog)
        return found

    def open_dialog(self, name, width, dialog) -> bool:
        """``dialog`` opened by pressing a control that opens it, after the
        menus that hold it, and its parts loaded; with ``--open``, every
        disclosure in it open too. The page's own wiring opens it, so that is
        looked at as well. Whether it opened."""

        openers = SELECTORS["dialog_opener"].format(id=dialog)
        place = self.page.evaluate(_REACH, openers)
        if place < 0:
            print(f"{name} at {width}: no control a reader can reach opens dialog {dialog!r}", flush=True)
            return False
        at = self.page.evaluate(_WINDOW_AT)
        self.page.locator(openers).nth(place).click()
        try:
            self.page.wait_for_function(_DIALOG_READY, arg=[dialog, _BUSY])
            # The page beneath as it was judged on its own: the press scrolled
            # the opener into view and left the pointer on it, hovered.
            self.page.evaluate(_WINDOW_TO, at)
            self.page.mouse.move(0, 0)
        except BrowserError as waited:
            print(
                f"{name} at {width}: dialog {dialog!r} did not open loaded ({str(waited).splitlines()[0]})", flush=True
            )
            self.close_dialog(dialog)
            return False
        self.settle()
        if _asked().switch("open"):
            self.open_everything(name, width)
        return True

    def shoot_dialog(self, name, width, dialog) -> list[Band]:
        """The window with ``dialog`` open and, when it scrolls within itself,
        the same scrolled down a band at a time, overlapping so a sticky head
        hides nothing (``.band-01.png``); the part of the dialog each holds,
        from the top of what it scrolls."""

        shot = _shot(name, width, f"dialog-{dialog}")
        self.page.screenshot(path=shot, animations="disabled")
        tall, band = self.page.evaluate(_DIALOG_EXTENT, dialog)
        tops = [*range(0, tall - band, band * 3 // 4), tall - band] if tall > band else []
        bands: list[Band] = [(shot.name, 0, band)]
        for number, top in enumerate(tops, start=1):
            self.page.evaluate(_DIALOG_SCROLL, [dialog, top])
            cut = shot.with_name(f"{shot.stem}.band-{number:02}.png")
            self.page.screenshot(path=cut, animations="disabled")
            bands.append((cut.name, top, band))
        if tops:
            self.page.evaluate(_DIALOG_SCROLL, [dialog, 0])
        self.shots.append((_shown(shot), _with(f"#modal-{dialog} open", len(tops))))
        # Cut in bands, a part is named by its band, as a page's is.
        return bands[1:] or bands

    def close_dialog(self, dialog):
        """``dialog`` closed with Escape, and the menus opened to reach it
        closed again, so the next is opened from the page as it was."""

        if not self.page.evaluate(_DIALOG_CLOSED, dialog):
            self.page.keyboard.press("Escape")
            self.page.wait_for_function(_DIALOG_CLOSED, arg=dialog)
        self.page.evaluate(_LEAVE)
        self.settle()

    def refused(self, name, width) -> str:
        """Why ``name`` at ``width`` is not a page to look at, if it is not."""

        try:
            self.open(name, width)
        except BrowserError as refused:
            return f"not a page ({str(refused).splitlines()[0]})"
        # A named POST's page is its refusal, so it answers that refusal's status.
        expected = posted(name).status if is_post(name) else 200
        served = is_estate(name) or is_post(name)
        if served and (said := self.answered.get(_address(self.page.url), (None, ""))) != (expected, ""):
            status, refusal = said
            if status != expected:
                self.unanswered_pages[name] = status
            return f"answered {status}{f', refused as a guess ({refusal})' if refusal else ''}, not looked at"
        if not self.page.locator("main").count():
            return "not a page of HQ's (no main), not looked at"
        return ""

    def draw_as_asked(self, name, width):
        """The page with ``--open`` and ``--expand`` done; ``open`` laid ``--css`` over it."""

        if _asked().switch("open"):
            self.open_everything(name, width)
        if _asked().switch("expand"):
            self.expand_rows()

    def open_operation(self, name, width) -> OpenedTag:
        """The operation ``--operation`` names opened on the reference as a
        reader opens it: its tag shown, and its body and every response
        opened with each property in them. An operation's own anchor would
        land on the reference's introduction, and its parts stay closed."""

        try:
            opened = opened_operation(served_document(self.page), _asked().text("operation"))
        except LookupError as unknown:
            raise AssertionError(f"--operation: {unknown}") from None
        if undrawn := open_tag(self.page, opened):
            print(f"{name} at {width}: the viewer drew nothing for {', '.join(undrawn)}", flush=True)
        self.settle()
        return opened

    def say_where(self, opened, bands):
        """The shot of ``opened`` captioned with the files that show it."""

        method, path = opened.name.split(" ", 1)
        boxes = self.page.evaluate(_OPERATION_BOX, [SELECTORS["reference"], f"/{method}{path}"])
        shown = _shown_in(boxes[0], bands) if boxes else "not drawn by the viewer"
        path, caption = self.shots[-1]
        self.shots[-1] = (path, f"{caption}, {opened.name} {shown}")

    def expand_rows(self):
        """Every row a reader can open, opened: only the toggles a reader can
        press, since one a row does not offer is drawn but not shown. A fold
        first, and once the page has marked the rows it shows, so each of them
        shows its own toggle before those are pressed. Settled last, so
        ``--eval`` reads the rows as the screenshot shows them."""

        self.as_a_reader(_PRESS_SHOWN, _FOLD_TOGGLES)
        self.settle()
        self.as_a_reader(_PRESS_SHOWN, ROW_TOGGLES)
        self.settle()

    def open_everything(self, name, width):
        """Every disclosure open, round after round until none is
        left closed: a disclosure a region brings in when another opens is
        opened once the region's answer has arrived. A region that never
        answers is said, and the page is looked at as it stands."""

        for _round in range(_OPEN_ROUNDS):
            if not self.as_a_reader(_OPEN_ONCE, _BUSY):
                return
            self.settle()
            try:
                self.page.wait_for_function(_ANSWERED, arg=_BUSY)
            except BrowserError as waited:
                print(f"{name} at {width}: --open left a region loading ({str(waited).splitlines()[0]})", flush=True)
                return

    def screenshot(self, name, width, part="", shows="") -> list[Band]:
        """The whole page, or a ``part`` of it open that ``shows`` says, and,
        when it is taller than the window, the same cut into window-sized
        bands beside it (``in_bands``). Shot once its extent holds still; one
        that never does is shot and cut from one reading of it, and the shot
        says so. The part of the page each file holds: its bands, or the
        shot when it has none."""

        shot = _shot(name, width, part)
        try:
            held = self.page.wait_for_function(_EXTENT_HELD, arg=shot.name, polling=_EXTENT_POLL)
            wide, tall = held.json_value()
        except BrowserError as moving:
            wide, tall = extent_of(self.page)
            shows = ", ".join(filter(None, [shows, f"its height never held still ({str(moving).splitlines()[0]})"]))
        return self.in_bands(shot, (wide, tall), shows)

    def in_bands(self, shot, extent, shows) -> list[Band]:
        """The page shot whole at ``shot`` and, when it is taller than the
        window, cut into window-sized bands beside it, so each can be read at
        once, each to ``TALLEST`` at most (``shoot_page``), all from the one
        ``extent``. A page that moved past that extent ends its bands where
        it went, and the shot says so. The part of the page each file holds."""

        wide, tall = extent
        cut = shoot_page(self.page, shot, extent=extent, animations="disabled")
        said = [shows, cut]
        files: list[Band] = []
        for number, band in enumerate(_bands(min(tall, TALLEST), self.page.viewport_size["height"]), start=1):
            path = shot.with_name(f"{shot.stem}.band-{number:02}.png")
            try:
                shoot_page(self.page, path, extent=(wide, tall), band=band, animations="disabled")
            except BrowserError as moved:
                said.append(f"its bands end at {band[0]} px, where it moved ({str(moved).splitlines()[0]})")
                break
            files.append((path.name, *band))
        self.shots.append((_shown(shot), _with(", ".join(filter(None, said)), len(files))))
        return files or [(shot.name, 0, min(tall, TALLEST))]

    def shoot_failure(self, name, width, said) -> list[str]:
        """``said`` as the finding, with the page as it stands kept beside the
        screenshots (``.failed.png``), in bands as any shot is, and its path printed."""

        path = _shot(name, width).with_suffix(".failed.png")
        try:
            self.in_bands(path, extent_of(self.page), "as it failed")
        except BrowserError as broke:
            print(f"{name} at {width}: {said}; no screenshot ({str(broke).splitlines()[0]})", flush=True)
        return [said]

    def beside_base(self, name, width, kind, said) -> str:
        """What was measured here, with the base's numbers beside it when
        there is a base."""

        if self.base is None:
            return said
        return beside(_asked().text("base"), said, self.base_measured.get((name, width), {}).get(kind))

    def measure_columns(self, name, width):
        """Every table's column widths here, printed and kept beside the
        screenshot (``.columns.txt``): why a column is starved or a table
        overflows, in numbers."""

        said = "\n".join(columns_line(name, width, table) for table in self.page.evaluate(COLUMNS))
        said = said or f"{name} at {width}: (no tables)"
        shot = _shot(name, width)
        shot.with_name(shot.stem + ".columns.txt").write_text(said + "\n", encoding="utf-8")
        self.measured.setdefault((name, width), {})["columns"] = said
        print(self.beside_base(name, width, "columns", said), flush=True)

    def evaluate_script(self, where, width) -> tuple[str, object]:
        """``--eval``'s script run on the page as it stands: what it threw, as
        a line under ``where``, or what it returned, each element in it kept
        in the page to be measured once the shot is taken."""

        try:
            # A handle, so an element the script returns stays the page's.
            handle = self.page.evaluate_handle(self.script)
            returned = self.page.evaluate(_RETURNED, [handle, _ELEMENT])
            handle.dispose()
        except BrowserError as broke:
            return f"{where} at {width}: --eval threw ({str(broke).splitlines()[0]})", None
        return "", returned

    def take_shot(self, name, width, take, state="", shows="", frame=None) -> list[Band]:
        """One shot ``take`` takes, with ``--eval``'s script run first on the
        page it shoots, ``state`` that shot's suffix and ``shows`` what it
        shows, and what the script returns printed and kept beside it
        (``.eval.txt``): a computed style or an ancestor chain read in one
        command. An element it returns is said as the shot file that shows
        it, the band where the shot is cut in bands (``take`` says what each
        holds, from the top of ``frame``'s scrolled content, or the page's).
        The page is settled after the script, so the shot draws what it
        changed. One that throws says so and the look goes on. ``--probe``
        reads the page the shot drew. What ``take`` said each file holds; a
        run that takes no shots (``shooting``) runs only ``--probe``."""

        where = f"{name} {shows}" if shows else name
        if not self.shooting:
            if self.probe:
                self.probe_beside(name, width, state, where)
            return []
        threw, returned = self.evaluate_script(where, width) if self.script else ("", None)
        if self.script:
            self.settle()
        bands = take()
        if self.script:
            boxes = functools.cache(lambda: self.page.evaluate(_RETURNED_BOXES, frame))
            said = threw or _evaluated(where, width, _placed(returned, boxes, bands))
            self.keep_beside(name, width, state, "eval", said)
        if self.probe:
            self.probe_beside(name, width, state, where)
        return bands

    def probe_beside(self, name, width, state, where):
        """``--probe`` run on the page as it stands, and what it said kept beside the shot."""

        [outcome] = self.outcomes([(self.probe_source, self.probe_arg)])
        self.keep_beside(name, width, state, "probe", _probed(where, width, self.probe, outcome))

    def keep_beside(self, name, width, state, kind, said):
        """What ``--eval`` or ``--probe`` said of one shot, printed with the
        base's beside it and kept beside the shot (``.eval.txt``, ``.probe.txt``)."""

        shot = _shot(name, width, state)
        shot.with_name(f"{shot.stem}.{kind}.txt").write_text(said + "\n", encoding="utf-8")
        kind = f"{kind} {state}" if state else kind
        self.measured.setdefault((name, width), {})[kind] = said
        print(self.beside_base(name, width, kind, said), flush=True)

    def tables(self, name):
        """What each table shows here, printed and kept beside the screenshot
        (``.tables.txt``), so two runs can be compared line by line. Not a
        check: it fails nothing."""

        said = f"{name} at {self.width}{' expanded' if _asked().switch('expand') else ''}\n"
        said += self.page.evaluate(TABLES) or "(no tables)"
        suffix = "-expanded.tables.txt" if _asked().switch("expand") else ".tables.txt"
        shot = _shot(name, self.width)
        shot.with_name(shot.stem + suffix).write_text(said + "\n", encoding="utf-8")
        print(said, flush=True)

    def survey(self, name):
        """Every check the layout gates hold, on any page at any width."""

        path = self.addresses.get(name, name)
        if not getattr(self, "said_checks", False):
            self.said_checks = True
            print(f"survey checks: {', '.join(survey_checks(path, self.width))}", flush=True)
        failed = self.findings(path, self.width)
        if failed:
            raise AssertionError("\n".join(f"{check}: {line}" for check, said in failed.items() for line in said))
