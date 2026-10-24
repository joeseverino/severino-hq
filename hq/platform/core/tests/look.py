"""A page as the browser gates draw it, for a person or an agent to look at.

It runs only as `mise run dev:look`, whose flags say what each does
(`mise run dev:look --help`), and reads what it was asked from that task's
`usage_*` environment, so a run through `manage` without it is refused. How
to use it is in docs/Development.md, under "Browser layout".
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import override
from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django.test import Client, override_settings
lazy from playwright.sync_api import Error as BrowserError

from hq.platform.core.speculation import NOT_A_NAVIGATION
from hq.platform.core.test_runner import stall_watch
from hq.platform.core.tests.browser.estate import ExampleEstateBrowserTests, navigation
from hq.platform.core.tests.browser.gate import BrowserGate, add_css
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.reference import REFERENCE, REFERENCE_DARK, render_reference, wait_for_reference
from hq.platform.core.tests.browser.stress import ALL_PAGES
from hq.platform.core.tests.browser.survey import AUDIT, survey_checks
from hq.platform.core.tests.browser.widths import PHONE_SCREEN
from hq.platform.core.tests.look_report import (
    Found,
    Measured,
    beside,
    block,
    closing,
    unmatched,
)
from hq.platform.core.tests.look_tables import COLUMNS, TABLES, columns_line
from hq.platform.core.tests.test_browser_derived_fixtures import derived_pages
lazy from hq.platform.core.example_estate import REQUEST, SSO, as_production, build_showcase

OUT = Path(settings.BASE_DIR) / "var" / "look"
# What a look against a base runs at the base as this checkout has it, so a
# finding is new for what the change drew rather than for how it was judged:
# this module and its report, and the rules and probes the survey holds.
LENS = (
    *(Path(__file__).resolve().with_name(name) for name in ("look.py", "look_report.py", "look_tables.py")),
    AUDIT.resolve(),
    *sorted((Path(__file__).resolve().parent / "browser").rglob("*.py")),
)
# A look against a base writes its whole report here and prints only its new
# findings and its verdict, so a long report is never cut short where it is read.
REPORT = Path(settings.BASE_DIR) / "var" / "gate" / "look.txt"
REFERENCE_PAGES = (REFERENCE, REFERENCE_DARK)
ESTATE = "estate"

# Request headers the browser sends that are not the view's to read. The
# answer goes back with its type alone, so it is never asked to be compressed.
_TRANSPORT = frozenset({"host", "cookie", "content-length", "connection", "accept-encoding"})

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
# Each popover's kind: its classes and the table and column it sits in. Popovers
# of one kind are drawn by one partial in one place, so one of them stands for
# all, and an opened survey costs a run per kind rather than per record.
_POPOVER_KINDS = """() => {
  const tables = [...document.querySelectorAll('table')];
  return [...document.querySelectorAll('[popover]')].map((popover) => {
    const table = popover.closest('table');
    return [popover.className, tables.indexOf(table), popover.closest('td, th')?.cellIndex ?? -1].join(' ');
  });
}"""
# The popover at a place in the page shown, and what names it in a finding;
# nothing for one that will not show.
_SHOW_POPOVER = """(place) => {
  const popover = document.querySelectorAll('[popover]')[place];
  try {
    popover.showPopover();
  } catch {
    return null;
  }
  return popover.id ? `#${popover.id}` : `popover ${place + 1}`;
}"""
_HIDE_POPOVER = "(place) => document.querySelectorAll('[popover]')[place].hidePopover()"
# No region a round of opening brought in is waiting on its answer.
_ANSWERED = "(busy) => [...document.querySelectorAll(busy)].every((region) => region.lookWaiting)"
# The rows a reader opens: the folds first, then each row on its own.
_FOLD_TOGGLES = "main button[data-fold-toggle]"
_ROW_TOGGLES = "main button[data-row-toggle]:not([data-fold-toggle])"
_PRESS_SHOWN = "(toggles) => document.querySelectorAll(toggles).forEach((t) => t.checkVisibility() && t.click())"
# A fold's rows are marked as cut, which shows their toggles, on tables.js's
# debounce (`markTablesSoon`), a timer the press sets. A timer set after it
# with no shorter delay fires after it, so this resolves once it has run.
_AFTER_THE_PAGES_TIMERS = "() => new Promise((done) => setTimeout(done, 250))"
# Rounds of opening, each after what the last one brought in has arrived.
_OPEN_ROUNDS = 10
_EXTENT = "() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]"

# Every dialog the page declares, by the id after `modal-` (partials/_modal.html).
_DIALOGS = """() => [...document.querySelectorAll('dialog.modal[id^="modal-"]')].map((dialog) => dialog.id.slice(6))"""
# The controls that open a dialog: a link the page's wiring intercepts, or a
# control that says it opens a dialog and names it.
_OPENERS = '[data-modal-open="{id}"], [aria-haspopup="dialog"][aria-controls="modal-{id}"]'
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
_ASKED = "usage_pages"
# Where a look at the base writes what it found, for the run that asked for it:
# a usage variable of the task's no person sets, given only by that run.
_FINDINGS = "usage_found_into"
# The instant the run that asked began, in seconds, handed to its base the
# same way: both draw with the clock frozen there.
_CLOCK = "usage_clock_at"


def _usage(variable: str) -> str:
    """What the task was given: mise exports each argument and flag of a
    task's usage as ``usage_<name>``, lower case, and an unset flag as nothing
    at all. Without the task's environment every flag would read as unset."""

    if _ASKED not in os.environ:
        raise RuntimeError(
            f"dev:look reads what it was asked from its task's environment, and {_ASKED} is not set:"
            " run it as `mise run dev:look -- <pages>`, never as a test label through manage"
        )
    return os.environ.get(variable, "")


def _listed(name: str) -> list[str]:
    return [part.strip() for part in _usage(name).split(",") if part.strip()]


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


def _write_found(path: Path, found: Found, measured: Measured) -> None:
    entries = [
        {"page": name, "width": width, "found": said, "measured": measured.get((name, width), {})}
        for (name, width), said in found.items()
    ]
    path.write_text(json.dumps(entries), encoding="utf-8")


def _read_found(path: Path) -> tuple[Found, Measured]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    found = {(entry["page"], entry["width"]): entry["found"] for entry in entries}
    return found, {(entry["page"], entry["width"]): entry["measured"] for entry in entries}


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


def _stop(signum, _frame):
    raise SystemExit(128 + signum)


@contextmanager
def _worktree(checkout: Path, ref: str) -> Iterator[Path]:
    """A detached worktree of ``ref`` in a temporary folder, removed with the
    folder however the block ends: a termination is raised as an exit, so it
    unwinds through here too."""

    folder = Path(tempfile.mkdtemp(prefix="hq-look-base-"))
    tree = folder / "checkout"
    git = ["git", "-C", str(checkout), "worktree"]
    previous = signal.signal(signal.SIGTERM, _stop)
    try:
        made = subprocess.run(
            [*git, "add", "--detach", "--quiet", str(tree), ref], capture_output=True, text=True, check=False
        )
        if made.returncode:
            raise AssertionError(f"--base {ref}: {made.stderr.strip()}")
        yield tree
    finally:
        subprocess.run([*git, "remove", "--force", str(tree)], capture_output=True, check=False)
        shutil.rmtree(folder, ignore_errors=True)
        subprocess.run([*git, "prune"], capture_output=True, check=False)
        signal.signal(signal.SIGTERM, previous)


def _relay(command: list[str], sink, ref: str, **options) -> None:
    """Run ``command``, each line it writes kept in ``sink`` and said here as
    it arrives, after ``ref``, so the base's pass reads apart from this one's
    and a hang there names its page. The command is killed if this run ends
    first."""

    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, **options
    ) as run:
        try:
            for line in run.stdout:
                sink.write(line)
                print(f"{ref}: {line}", end="", flush=True)
        except BaseException:
            run.kill()
            raise


def _look_at(ref: str, rule: str, clock_at: float) -> tuple[Found, Measured]:
    """What this run finds and measures, looked for at ``ref``: the same
    task's run, in a worktree of it, through this checkout's copy of the
    lens (`LENS`) so both are judged alike. Its environment is this run's, but for a
    database of its own, this run's clock, and the CSS already read and the
    script's absolute path, since a relative path names this checkout."""

    checkout = Path(settings.BASE_DIR)
    with _worktree(checkout, ref) as tree:
        for lens in LENS:
            copy = tree / lens.relative_to(checkout.resolve())
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(lens, copy)
        found, log = tree.parent / "found.json", tree.parent / "look.log"
        env = {name: value for name, value in os.environ.items() if name not in {"usage_base", "VIRTUAL_ENV"}}
        env |= {
            _FINDINGS: str(found),
            _CLOCK: str(clock_at),
            "SEVERINO_DATABASE_PATH": str(tree.parent / "look.sqlite3"),
            "usage_rule": rule,
            "usage_css": _css(_usage("usage_css")),
        }
        if script := _usage("usage_eval"):
            env["usage_eval"] = str(Path(settings.BASE_DIR, script).resolve())
        print(f"Looking at {ref} first, in {tree}", flush=True)
        # The run there is watched by its own stall watch.
        with log.open("w", encoding="utf-8") as sink, stall_watch.paused():
            # The command this run was started with, run there by its own environment.
            _relay(["uv", "run", "--group", "browser", "python", *sys.argv], sink, ref, cwd=tree, env=env)
        if not found.is_file():
            said = log.read_text(encoding="utf-8").splitlines()[-40:]
            raise AssertionError(f"The look at {ref} did not finish:\n" + "\n".join(said))
        return _read_found(found)


def _estate_name(path: str) -> str:
    return "/".join(filter(None, (ESTATE, path.strip("/"))))


def _is_estate(name: str) -> bool:
    return name == ESTATE or name.startswith(f"{ESTATE}/")


def _estate_path(name: str) -> str:
    """The address an estate page is asked by: its path ends in a slash, and
    its query, if any, follows untouched."""

    path, _, query = name.removeprefix(ESTATE).partition("?")
    rest = path.strip("/")
    return (f"/{rest}/" if rest else "/") + (f"?{query}" if query else "")


def _address(url: str) -> str:
    """A URL's path and query: where it is on whichever origin."""

    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _shot(name: str, width: int, dialog: str = "") -> Path:
    shot = OUT / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-')}-{width}.png"
    return shot.with_name(f"{shot.stem}.dialog-{dialog}.png") if dialog else shot


class Look(LayoutBrowserTests, ExampleEstateBrowserTests):
    """The layout gate's rules and pages, with the example estate's way of
    loading a page whose parts arrive after it."""

    @classmethod
    def setUpClass(cls):
        cls.clock_at = float(_usage(_CLOCK) or round(time.time()))
        # The base first, while nothing of this run's holds memory but Django.
        ref = _usage("usage_base")
        cls.base, cls.base_measured = _look_at(ref, cls.rule_asked(), cls.clock_at) if ref else (None, {})
        super().setUpClass()

    @override
    def start(self, **options):
        super().start(**options)
        # Date reads the same instant all run, timers still run.
        self.page.clock.set_fixed_time(self.clock_at)

    @classmethod
    def rule_asked(cls):
        return _usage("usage_rule") or ("survey" if _usage("usage_base") else "")

    @classmethod
    def render(cls):
        estate = [_estate_name(path) for path in navigation()]
        stress = [*ALL_PAGES, *derived_pages()]
        known = [*stress, *REFERENCE_PAGES, *estate]
        named = {"all": known, f"{ESTATE}/all": estate}
        pages = list(dict.fromkeys(page for asked in _listed("usage_pages") for page in named.get(asked, [asked])))
        # Any address of the example estate is a page, not only its navigation's.
        unknown = sorted(name for name in set(pages) - set(known) if not _is_estate(name))
        if unknown:
            raise AssertionError(f"No page {unknown}. Pages: {', '.join(known)}, or estate/<any address>")
        cls.asked = pages
        cls.addresses = {name: _estate_path(name) for name in {*estate, *filter(_is_estate, pages)}}
        rendered: dict = {}
        if set(pages) & set(stress):
            rendered |= LayoutBrowserTests.render()
        if set(pages) & set(REFERENCE_PAGES):
            rendered |= render_reference()
        # Last: the stress estates are rolled back, the example estate is
        # committed, and both sign in as the same owner.
        cls.estate_client = cls.serve_estate() if any(map(_is_estate, pages)) else None
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
        # this one builds its estates into the database the runner made.
        stack.callback(call_command, "flush", interactive=False, verbosity=0)
        # Django takes Playwright's event loop for async code sharing a
        # connection. It is this one thread, and nothing else runs while a
        # request is answered.
        stack.enter_context(mock.patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "1"}))
        stack.enter_context(as_production())
        stack.enter_context(override_settings(SEVERINO_POSTURE="advanced"))
        client = Client()
        client.force_login(build_showcase(), backend=SSO)
        cls.answered = {}
        return client

    # Whether the page in the browser is the example estate's. A stress page
    # and an estate page can share an address (`projects`), so the address
    # alone cannot say whose it is.
    on_estate = False

    def answer(self, route):
        """A stress page or a static file, as the layout gate answers it:
        `respond` answers the estate's pages itself."""

        BrowserGate.answer(self, route)

    def respond(self, route):
        request = route.request
        parts = urlsplit(request.url)
        if (
            self.estate_client is None
            or parts.path.startswith(settings.STATIC_URL)
            or (not self.on_estate and parts.path.strip("/") in self.pages)
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
        # Marked as a guess, so a page that would act or reach out is refused.
        headers["HTTP_SEC_PURPOSE"] = "prefetch"
        # A browser route cannot answer with a redirect, so it is followed here.
        response = self.estate_client.get(address, secure=True, follow=True, **{**headers, **REQUEST})
        self.answered[address] = (response.status_code, response.get("X-Speculation-Refused", ""))
        body = b"".join(response.streaming_content) if response.streaming else response.content
        # The event loop's own connection, closed rather than left to leak.
        connections.close_all()
        route.fulfill(
            status=response.status_code,
            headers={"content-type": response.get("Content-Type", "text/html")},
            body=body,
        )

    def visit(self, path, width):
        self.on_estate = True
        super().visit(path, width)

    def open(self, name, width):
        if _is_estate(name):
            self.visit(self.addresses[name], width)
            return
        self.on_estate = False
        super().open(name, width)
        if name in REFERENCE_PAGES and not _usage("usage_no_script"):
            wait_for_reference(self.page)

    def test_look(self):
        widths, rule = self.asked_for()
        # A page's own policy refuses a stylesheet it did not name; `--css` is one.
        self.start(java_script_enabled=not _usage("usage_no_script"), bypass_csp=bool(self.css))
        if _usage("usage_dark"):
            self.page.emulate_media(color_scheme="dark")
        OUT.mkdir(parents=True, exist_ok=True)
        if _usage("usage_deep"):
            self.follow(widths[0])
        print(f"Screenshots in {OUT}", flush=True)
        self.measured: Measured = {}
        found = self.look_at_every_page(widths, rule)
        if into := _usage(_FINDINGS):
            _write_found(Path(into), found, self.measured)
        if not rule:
            return
        ref = _usage("usage_base")
        report, printed, failed = closing(rule, found, self.base, ref)
        head = _commit("HEAD") + (f" against {ref} at {_commit(ref)}" if self.base is not None else "")
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(f"{head}\n{report}\n", encoding="utf-8")
        print(f"Page by page: {REPORT}", *printed, sep="\n", flush=True)
        if failed:
            self.fail(printed[-1])

    def look_at_every_page(self, widths, rule) -> Found:
        """Each page at each width, said as it starts, so a look that hangs
        names where; its findings kept beside its screenshot, and said once it
        has been looked at when no rule asks for a report."""

        found: Found = {}
        for name in self.asked:
            for width in widths:
                stall_watch.mark(f"dev:look {name} at {width}")
                print(f"Looking at {name} at {width}", flush=True)
                said = found[name, width] = self.look(name, width, rule)
                report = _shot(name, width).with_suffix(".txt")
                if said:
                    report.write_text("\n".join([f"{name} at {width}", *said]) + "\n", encoding="utf-8")
                else:
                    report.unlink(missing_ok=True)
            if not rule:
                print(block(name, {width: found[name, width] for width in widths}, rule), flush=True)
        return found

    def follow(self, width):
        """Every page the estate pages asked for link to, one per address."""

        seen = {urlsplit(self.addresses[name]).path for name in self.asked if _is_estate(name)}
        for name in [name for name in self.asked if _is_estate(name)]:
            stall_watch.mark(f"dev:look following the links of {name}")
            self.visit(self.addresses[name], width)
            for address in self.page.evaluate(_LINKS, NOT_A_NAVIGATION):
                path = urlsplit(address).path
                if path in seen or path.startswith(settings.STATIC_URL):
                    continue
                seen.add(path)
                followed = _estate_name(address)
                self.addresses[followed] = address
                self.asked.append(followed)

    def asked_for(self):
        """The widths and rule the task was given, each checked, and the CSS it
        adds."""

        widths = [int(width) for width in _listed("usage_widths")] or [PHONE_SCREEN[0], 820, 1360]
        rule = self.rule_asked()
        self.assertTrue(not rule or callable(getattr(self, rule, None)), f"No rule named {rule!r}")
        self.css = _css(_usage("usage_css"))
        self.script = _script(_usage("usage_eval"))
        self.dialogs = _listed("usage_dialog")
        unnamed = [dialog for dialog in self.dialogs if not re.fullmatch(r"[\w-]+", dialog)]
        self.assertFalse(unnamed, f"--dialog takes dialog ids (connection) or all, not {unnamed}")
        return widths, rule

    def look(self, name, width, rule) -> list[str]:
        """Screenshot ``name`` at ``width``, and what ``rule`` finds there."""

        _shot(name, width).with_suffix(".failed.png").unlink(missing_ok=True)
        if refused := self.refused(name, width):
            return self.shoot_failure(name, width, refused)
        self.draw_as_asked(name, width)
        # Before the shot, so what a script changes is seen.
        if self.script:
            self.evaluate_script(name, width)
        self.screenshot(name, width)
        if _usage("usage_columns"):
            self.measure_columns(name, width)
        self.width = width
        said = self.judged(name, width, rule) if rule else []
        if rule and _usage("usage_open"):
            said += self.with_each_popover(name, width, rule, said)
        return said + self.with_each_dialog(name, width, rule, said)

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
        """What ``rule`` finds with one popover of each kind shown on its own,
        as a reader shows one, past what it finds on the page ``beneath``.
        Shown all at once they would lie over one another, which no reader
        sees, and where each lands would hang on which drew last."""

        kinds = self.page.evaluate(_POPOVER_KINDS)
        judged, found = set(), []
        for place, kind in enumerate(kinds):
            if kind in judged or (shown := self.page.evaluate(_SHOW_POPOVER, place)) is None:
                continue
            judged.add(kind)
            self.settle()
            said = self.judged(name, width, rule)
            self.page.evaluate(_HIDE_POPOVER, place)
            alike = kinds.count(kind) - 1
            named = f"{shown} and {alike} like it" if alike else shown
            found += [f"with {named} shown: {line}" for line in unmatched(said, beneath)]
        return found

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
            self.shoot_dialog(name, width, dialog)
            if self.script:
                self.evaluate_script(name, width, dialog)
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

        openers = _OPENERS.format(id=dialog)
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
        if _usage("usage_open"):
            self.open_everything(name, width)
        return True

    def shoot_dialog(self, name, width, dialog):
        """The window with ``dialog`` open and, when it scrolls within itself,
        the same scrolled down a band at a time, overlapping so a sticky head
        hides nothing (``.band-01.png``)."""

        shot = _shot(name, width, dialog)
        for stale in OUT.glob(f"{shot.stem}.band-*.png"):
            stale.unlink()
        self.page.screenshot(path=shot, animations="disabled")
        tall, band = self.page.evaluate(_DIALOG_EXTENT, dialog)
        if tall <= band:
            return
        for number, top in enumerate([*range(0, tall - band, band * 3 // 4), tall - band], start=1):
            self.page.evaluate(_DIALOG_SCROLL, [dialog, top])
            self.page.screenshot(path=shot.with_name(f"{shot.stem}.band-{number:02}.png"), animations="disabled")
        self.page.evaluate(_DIALOG_SCROLL, [dialog, 0])

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
        if _is_estate(name) and (said := self.answered.get(_address(self.page.url))) != (200, ""):
            return f"answered {said}, not looked at"
        if not self.page.locator("main").count():
            return "not a page of HQ's (no main), not looked at"
        return ""

    def draw_as_asked(self, name, width):
        """The page with ``--css`` added, and ``--open`` and ``--expand`` done."""

        if self.css:
            add_css(self.page, self.css)
        if _usage("usage_open"):
            self.open_everything(name, width)
        if _usage("usage_expand"):
            self.expand_rows()

    def expand_rows(self):
        """Every row a reader can open, opened: only the toggles a reader can
        press, since one a row does not offer is drawn but not shown. A fold
        first, and once the page has marked the rows it shows, so each of them
        shows its own toggle before those are pressed."""

        self.page.evaluate(_PRESS_SHOWN, _FOLD_TOGGLES)
        self.page.evaluate(_AFTER_THE_PAGES_TIMERS)
        self.settle()
        self.page.evaluate(_PRESS_SHOWN, _ROW_TOGGLES)

    def open_everything(self, name, width):
        """Every disclosure open, round after round until none is
        left closed: a disclosure a region brings in when another opens is
        opened once the region's answer has arrived. A region that never
        answers is said, and the page is looked at as it stands."""

        for _round in range(_OPEN_ROUNDS):
            if not self.page.evaluate(_OPEN_ONCE, _BUSY):
                return
            self.settle()
            try:
                self.page.wait_for_function(_ANSWERED, arg=_BUSY)
            except BrowserError as waited:
                print(f"{name} at {width}: --open left a region loading ({str(waited).splitlines()[0]})", flush=True)
                return

    def screenshot(self, name, width):
        """The whole page, and, when it is taller than the window, the same
        cut into window-sized bands beside it, so each can be read at once."""

        shot = _shot(name, width)
        for stale in OUT.glob(f"{shot.stem}.band-*.png"):
            stale.unlink()
        self.page.screenshot(path=shot, full_page=True, animations="disabled")
        wide, tall = self.page.evaluate(_EXTENT)
        for number, (top, height) in enumerate(_bands(tall, self.page.viewport_size["height"]), start=1):
            self.page.screenshot(
                path=shot.with_name(f"{shot.stem}.band-{number:02}.png"),
                full_page=True,
                clip={"x": 0, "y": top, "width": wide, "height": height},
                animations="disabled",
            )

    def shoot_failure(self, name, width, said) -> list[str]:
        """``said`` as the finding, with the page as it stands kept beside the
        screenshots (``.failed.png``) and its path printed."""

        path = _shot(name, width).with_suffix(".failed.png")
        try:
            self.page.screenshot(path=path, full_page=True, animations="disabled")
        except BrowserError as broke:
            print(f"{name} at {width}: {said}; no screenshot ({str(broke).splitlines()[0]})", flush=True)
        else:
            print(f"{name} at {width}: {said}; screenshot {path}", flush=True)
        return [said]

    def beside_base(self, name, width, kind, said) -> str:
        """What was measured here, with the base's numbers beside it when
        there is a base."""

        if self.base is None:
            return said
        return beside(_usage("usage_base"), said, self.base_measured.get((name, width), {}).get(kind))

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

    def evaluate_script(self, name, width, dialog=""):
        """What ``--eval``'s script returns here, with ``dialog`` open if one
        is named, printed and kept beside its screenshot (``.eval.txt``): a
        computed style or an ancestor chain read in one command. One that
        throws says so and the look goes on."""

        where = f"{name} with #modal-{dialog} open" if dialog else name
        try:
            said = _evaluated(where, width, self.page.evaluate(self.script))
        except BrowserError as threw:
            said = f"{where} at {width}: --eval threw ({str(threw).splitlines()[0]})"
        shot = _shot(name, width, dialog)
        shot.with_name(shot.stem + ".eval.txt").write_text(said + "\n", encoding="utf-8")
        kind = f"eval dialog-{dialog}" if dialog else "eval"
        self.measured.setdefault((name, width), {})[kind] = said
        print(self.beside_base(name, width, kind, said), flush=True)

    def tables(self, name):
        """What each table shows here, printed and kept beside the screenshot
        (``.tables.txt``), so two runs can be compared line by line. Not a
        check: it fails nothing."""

        said = f"{name} at {self.width}{' expanded' if _usage('usage_expand') else ''}\n"
        said += self.page.evaluate(TABLES) or "(no tables)"
        suffix = "-expanded.tables.txt" if _usage("usage_expand") else ".tables.txt"
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
