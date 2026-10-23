"""A page as the browser gates draw it, for a person or an agent to look at.

`mise run dev:look -- <pages>` opens pages at a few widths, saves a screenshot
of each under var/look/, and, given `--rule`, runs that rule of
`LayoutBrowserTests` there and prints what it finds, page by page and width by
width, closing on one verdict that names every page it failed on. `--css` adds
CSS after the page's own, as text or a file of it: how a new rule is shown to
fail on the defect it was written for before it lands. Not a gate: it fails
only when the rule it was asked to run fails.

`--base <ref>` runs the same look at another commit first, in a temporary
worktree of it with this checkout's copy of this module, and then prints only
what is new here and what is no longer found: what a change caused, apart
from what was already there. Findings are matched with their numbers
stripped, so one whose row or pixel count moved is neither. With `--columns`
or `--eval`, a line the base measured otherwise follows this checkout's.

Each run's clock is frozen at the moment the run that asked began, its base's
too, so a time a script reads is drawn alike in both; screenshots are taken
with animations finished. A page taller than the window is also cut into
window-sized bands beside its full screenshot (`<page>-<width>.band-01.png`),
and a page that could not be looked at is shot as it stands
(`<page>-<width>.failed.png`), its path printed.

What it was asked comes from the task's `usage_*` environment, so it runs only
as the task: a run through `manage` without it is refused rather than guessed.

A page is one of the stress estate's (sparse, dense, and every list filled
from its model under derived/), the API reference
(`api/docs`, `api/docs/dark`), or a page the example estate's navigation
reaches, named for its address under `estate` (`estate`, `estate/finance`).
`--deep` adds every page those link to: each list's records, the command forms
they open. Only the sources the pages asked for are rendered.

The example estate is answered as the browser asks, by HQ's own views over the
run's throwaway test database, each request marked as a prefetch: a page
that would act or reach out when fetched is refused there
(`hq.platform.core.speculation`), and is reported rather than looked at.

`--rule survey` runs every check the layout gates hold, at whatever page and
width it is asked for; markup a vendored script draws is not judged.
`--columns` prints every table's column widths: as drawn, and at
min-content and at max-content, measured on an offscreen copy of the table;
a table an open row holds says it is drawn at the held widths, and its
min and max are measured without the hold.
`--eval <file.js>` runs the script in that file on each page at each width,
once it is drawn as asked, and prints what it returns: a function, called with
nothing, or an expression.
`--rule tables` records what each table shows (its columns, the rows that
open, the gutter, the sort controls) beside each screenshot, for comparing
two runs.
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
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from itertools import groupby, zip_longest
from pathlib import Path
from typing import override
from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django.test import Client, override_settings
lazy from playwright.sync_api import Error as BrowserError

from hq.platform.core.browser_floors import NAME, judged, measured
from hq.platform.core.browser_tests import (
    ALL_PAGES,
    PHONE_SCREEN,
    REFERENCE,
    REFERENCE_DARK,
    BrowserGate,
    LayoutBrowserTests,
    add_css,
    render_reference,
    survey_checks,
    wait_for_reference,
)
from hq.platform.core.estate_browser_tests import ExampleEstateBrowserTests, navigation
from hq.platform.core.speculation import NOT_A_NAVIGATION
from hq.platform.core.tests.test_browser_derived_fixtures import derived_pages
lazy from hq.platform.core.example_estate import REQUEST, SSO, as_production, build_showcase

OUT = Path(settings.BASE_DIR) / "var" / "look"
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
# Every disclosure and popover not yet open, opened, and how many: an
# exclusive group's name is dropped and a popover is shown as a manual one,
# so opening one closes no other.
_OPEN_ONCE = """(busy) => {
  // A region already waiting is not one this round brings in.
  document.querySelectorAll(busy).forEach((region) => { region.lookWaiting = true; });
  let opened = 0;
  document.querySelectorAll('details:not([open])').forEach((details) => {
    details.removeAttribute('name');
    details.open = true;
    opened += 1;
  });
  document.querySelectorAll('[popover]:not(:popover-open)').forEach((popover) => {
    popover.popover = 'manual';
    try {
      popover.showPopover();
      opened += 1;
    } catch {}
  });
  return opened;
}"""
# No region a round of opening brought in is waiting on its answer.
_ANSWERED = "(busy) => [...document.querySelectorAll(busy)].every((region) => region.lookWaiting)"
# Rounds of opening, each after what the last one brought in has arrived.
_OPEN_ROUNDS = 10
_EXTENT = "() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]"

# Each table as a reader finds it: which columns show, which are gone and
# whether for being blank or for want of room, which rows offer to open and
# have the gutter for it, the sort controls, and what each open row lists.
_TABLES = (
    "() => {"
    + NAME
    + r"""
  const blank = (cell) => !cell || (/^[\s\u2013\u2014-]*$/.test(cell.textContent)
    && !cell.querySelector(':not(.empty-value, .empty-value *, br)'));
  const seen = (el) => el.checkVisibility();
  const pad = (el) => Math.round(parseFloat(getComputedStyle(el).paddingInlineStart));
  const tally = (values) => {
    const counts = new Map();
    values.forEach((value) => counts.set(value, (counts.get(value) || 0) + 1));
    return [...counts].map(([value, count]) => (count > 1 ? `${value} x${count}` : value)).join('; ');
  };
  const out = [];
  document.querySelectorAll('main table').forEach((table, index) => {
    if (!seen(table)) return;
    const region = table.closest('.table-scroll');
    const label = region?.getAttribute('aria-label') || table.caption?.textContent.trim() || '';
    const rows = [...table.querySelectorAll(':scope > tbody > tr:not([data-row-detail])')].filter(seen);
    const records = rows.filter((row) => !row.classList.contains('row-group'));
    const headings = [...(table.tHead?.rows[table.tHead.rows.length - 1]?.cells || [])];
    const columns = headings.map((heading, column) => {
      const said = name(heading) || '(unnamed)';
      if (seen(heading)) return said;
      const full = records.filter((row) => row.cells.length === headings.length);
      return (full.length && full.every((row) => blank(row.cells[column])) ? '~' : '-') + said;
    });
    const toggles = records.filter((row) => [...row.querySelectorAll('[data-row-toggle]')].some(seen));
    // The column a shown toggle stands in, else the one that names the row:
    // the gutter is its inset.
    const shown = toggles.map((row) => [...row.querySelectorAll('[data-row-toggle]')].find(seen)?.closest('td, th')).find(Boolean);
    const column = shown ? shown.cellIndex
      : Math.max(0, headings.findIndex((heading) => !heading.classList.contains('select-column') && !heading.querySelector('input[type=checkbox]')));
    const gutter = records.map((row) => row.cells[column]).filter(Boolean).map(pad);
    const headingPad = headings[column] ? pad(headings[column]) : '-';
    const at = (rows) => (rows.length === records.length ? 'every row' : rows.length ? rows.map((row) => records.indexOf(row) + 1).join(',') : 'none');
    const open = [...table.querySelectorAll(':scope > tbody > tr[data-row-detail]')].map(
      (detail) => [...detail.querySelectorAll('.row-columns > dt')].map((term) => term.textContent.trim()).join(', ') || '(below only)',
    );
    const r = region?.getBoundingClientRect();
    const lines = [
      `[${index + 1}] ${label}${region ? ` region ${Math.round(r.width)}` : ''} table ${Math.round(table.getBoundingClientRect().width)}`
        + `${region?.classList.contains('is-pane') ? ' pane' : ''}`
        + ` cap ${getComputedStyle(table).getPropertyValue('--identifier-max').trim()}`,
      `  columns: ${columns.join(' | ') || '(no headings)'}`,
      `  widths: ${headings.filter(seen).map((heading) => Math.round(heading.getBoundingClientRect().width)).join(' ')}`,
      `  rows ${records.length}; toggles: ${at(toggles)}; sort ${[...table.querySelectorAll(':scope > thead .table-sort-link')].filter(seen).length}`,
      `  inset of the toggle column: heading ${headingPad}; cells ${tally(gutter) || '-'}`,
    ];
    if (open.length) lines.push(`  open: ${tally(open)}`);
    out.push(lines.join('\n'));
  });
  return out.join('\n');
}"""
)


# Each shown table's columns: the width each is drawn at, and the widths it
# would take at min-content and at max-content, read from copies laid out
# beside the table (so its container and cascade are the table's own), off
# screen and removed at once. The drawn table is never restyled.
_COLUMNS = (
    "() => {"
    + NAME
    + r"""
  const across = (table) => [...((table.tHead?.rows[table.tHead.rows.length - 1] || table.rows[0])?.cells || [])];
  const width = (el) => Math.round(el.getBoundingClientRect().width);
  const laidOut = (table, size) => {
    const copy = table.cloneNode(true);
    copy.setAttribute('aria-hidden', 'true');
    copy.querySelectorAll('[id]').forEach((el) => el.removeAttribute('id'));
    // Let go of what an open row holds (tables.js), so the copy is sized by
    // its content.
    copy.querySelectorAll(':scope > thead > tr > th').forEach((heading) => heading.style.removeProperty('width'));
    delete copy.dataset.rowsHeld;
    const style = {
      position: 'absolute', 'inset-inline-start': '-100000px', 'inset-block-start': '0', visibility: 'hidden',
      'table-layout': 'auto', 'inline-size': size, 'min-inline-size': '0', 'max-inline-size': 'none',
    };
    Object.entries(style).forEach(([property, value]) => copy.style.setProperty(property, value, 'important'));
    table.after(copy);
    try {
      return { table: width(copy), cells: across(copy).map(width) };
    } finally {
      copy.remove();
    }
  };
  const shown = [...document.querySelectorAll('main table')].map((table, index) => ({ table, index }))
    .filter(({ table }) => table.checkVisibility());
  return shown.map(({ table, index }) => {
    const region = table.closest('.table-scroll');
    const least = laidOut(table, 'min-content');
    const most = laidOut(table, 'max-content');
    return {
      index: index + 1,
      label: region?.getAttribute('aria-label') || table.caption?.textContent.trim() || '',
      region: region ? width(region) : null,
      drawn: width(table),
      // An open row holds the columns at the widths they had (tables.js).
      held: 'rowsHeld' in table.dataset,
      min: least.table,
      max: most.table,
      columns: across(table).map((cell, column) => ({
        name: name(cell) || '(unnamed)',
        shown: cell.checkVisibility(),
        drawn: width(cell),
        min: least.cells[column],
        max: most.cells[column],
      })),
    };
  });
}"""
)


def _columns(page: str, width: int, table: dict) -> str:
    """One table's columns on one line: each drawn width, and the widths it
    would take at min-content and at max-content; a column the table does not
    show is named as gone. A table an open row holds says that it is drawn
    at the held widths, which its min and max are not."""

    label = f' "{table["label"]}"' if table["label"] else ""
    region = f" in {table['region']}" if table["region"] is not None else ""
    held = (
        "; held by an open row: drawn at the widths it had before, min and max measured without the hold"
        if table["held"]
        else ""
    )
    head = (
        f"{page} table {table['index']}{label} at {width}"
        f" (drawn {table['drawn']}{region}; min {table['min']}, max {table['max']}{held})"
    )
    columns = [
        f"{column['name']} {column['drawn']} (min {column['min']}, max {column['max']})"
        if column["shown"]
        else f"{column['name']} gone"
        for column in table["columns"]
    ]
    return f"{head}: {' · '.join(columns) or '(no headings)'}"


# The task's one required argument: mise exports it on every run of the task,
# so a process without it was not started by the task.
_ASKED = "usage_pages"
# Where a look at the base writes what it found, for the run that asked for it:
# a usage variable of the task's no person sets, given only by that run.
_FINDINGS = "usage_found_into"
# The instant the run that asked began, in seconds, handed to its base the
# same way: both draw with the clock frozen there.
_CLOCK = "usage_clock_at"
# What a look found, by page and width, each finding a line.
type Found = dict[tuple[str, int], list[str]]
# What a look measured, by page and width: `columns` and `eval`, as printed.
type Measured = dict[tuple[str, int], dict[str, str]]


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


def _block(name: str, found: dict[int, list[str]], rule: str) -> str:
    """One page's findings, under its name, width by width."""

    lines = [name]
    for width, said in found.items():
        verdict = f"{len(said)} found" if said else "holds" if rule else "looked at"
        lines.append(f"  {width}: {verdict}")
        lines.extend(f"    {line}" for line in said)
    return "\n".join(lines)


def _grouped(found: Found, rule: str) -> str:
    """Findings page by page, in the order they were looked at."""

    return "\n".join(
        _block(name, {width: said for (_, width), said in entries}, rule)
        for name, entries in groupby(found.items(), key=lambda entry: entry[0][0])
    )


def _failed(found: Found) -> list[str]:
    return list(dict.fromkeys(name for (name, _), said in found.items() if said))


def _verdict(rule: str, found: Found) -> str:
    pages = list(dict.fromkeys(name for name, _ in found))
    widths = ", ".join(map(str, dict.fromkeys(width for _, width in found)))
    failed = _failed(found)
    if not failed:
        return f"{rule} holds on every page at {widths}"
    return f"{rule} fails on {len(failed)} of {len(pages)} pages: {', '.join(failed)}"


# A number in a finding: a row's place, a width, a count of pixels.
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _unmatched(lines: list[str], against: list[str]) -> list[str]:
    """The lines with no counterpart in ``against``, read with their numbers
    stripped, one counterpart each: a finding whose row moved or whose
    overflow grew by a pixel is the same finding."""

    left = Counter(_NUMBER.sub("#", line) for line in against)
    unmatched = []
    for line in lines:
        shape = _NUMBER.sub("#", line)
        if left[shape]:
            left[shape] -= 1
        else:
            unmatched.append(line)
    return unmatched


def _compare(base: Found, branch: Found) -> tuple[Found, Found]:
    """What the branch finds that the base does not, and what the base found
    on a page the branch looked at too that the branch no longer does."""

    new = {key: _unmatched(said, base.get(key, [])) for key, said in branch.items()}
    fixed = {key: _unmatched(base[key], said) for key, said in branch.items() if key in base}
    return {key: said for key, said in new.items() if said}, {key: said for key, said in fixed.items() if said}


def _against(ref: str, rule: str, base: Found, branch: Found) -> tuple[str, bool]:
    """The report of a look against a base, and whether it found anything new."""

    new, fixed = _compare(base, branch)
    parts = []
    if new:
        parts.append(f"New against {ref}:\n{_grouped(new, rule)}")
    if fixed:
        parts.append(f"Fixed against {ref}:\n{_grouped(fixed, rule)}")
    count = sum(map(len, new.values()))
    fixes = f"; {sum(map(len, fixed.values()))} fixed" if fixed else ""
    if new:
        verdict = f"{rule} against {ref}: {count} new on {', '.join(_failed(new))}{fixes}"
    else:
        verdict = f"{rule} against {ref}: nothing new on {len(dict.fromkeys(n for n, _ in branch))} pages{fixes}"
    return "\n".join([*parts, verdict]), bool(new)


def _beside(ref: str, ours: str, theirs: str | None) -> str:
    """What this checkout measured, each line followed by the one the base
    measured in its place when the two differ, marked with ``ref``."""

    if theirs is None:
        return f"{ours}\n{ref}: (not measured)"
    lines = []
    for here, there in zip_longest(ours.splitlines(), theirs.splitlines()):
        if here is not None:
            lines.append(here)
        if there != here:
            lines.append(f"{ref}: {'(no such line)' if there is None else there.strip()}")
    return "\n".join(lines)


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


def _look_at(ref: str, rule: str, clock_at: float) -> tuple[Found, Measured]:
    """What this run finds and measures, looked for at ``ref``: the same
    task's run, in a worktree of it, through this checkout's copy of this
    module so both report alike. Its environment is this run's, but for a
    database of its own, this run's clock, and the CSS already read and the
    script's absolute path, since a relative path names this checkout."""

    checkout = Path(settings.BASE_DIR)
    with _worktree(checkout, ref) as tree:
        lens = Path(__file__).resolve()
        shutil.copyfile(lens, tree / lens.relative_to(checkout.resolve()))
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
        with log.open("w", encoding="utf-8") as sink:
            # The command this run was started with, run there by its own environment.
            subprocess.run(
                ["uv", "run", "--group", "browser", "python", *sys.argv],
                cwd=tree,
                env=env,
                stdout=sink,
                stderr=subprocess.STDOUT,
                check=False,
            )
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


def _shot(name: str, width: int) -> Path:
    return OUT / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-')}-{width}.png"


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
        if self.base is not None:
            report, new = _against(_usage("usage_base"), rule, self.base, found)
            print(report, flush=True)
            if new:
                self.fail(report.splitlines()[-1])
        elif rule:
            verdict = _verdict(rule, found)
            if _failed(found):
                self.fail(verdict)
            print(verdict, flush=True)

    def look_at_every_page(self, widths, rule) -> Found:
        """Each page at each width, its findings kept beside its screenshot and
        printed once it has been looked at, unless they are to be compared."""

        found: Found = {}
        for name in self.asked:
            for width in widths:
                said = found[name, width] = self.look(name, width, rule)
                report = _shot(name, width).with_suffix(".txt")
                if said:
                    report.write_text("\n".join([f"{name} at {width}", *said]) + "\n", encoding="utf-8")
                else:
                    report.unlink(missing_ok=True)
            if self.base is None:
                print(_block(name, {width: found[name, width] for width in widths}, rule), flush=True)
        return found

    def follow(self, width):
        """Every page the estate pages asked for link to, one per address."""

        seen = {urlsplit(self.addresses[name]).path for name in self.asked if _is_estate(name)}
        for name in [name for name in self.asked if _is_estate(name)]:
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
        return widths, rule

    def look(self, name, width, rule) -> list[str]:
        """Screenshot ``name`` at ``width``, and what ``rule`` finds there."""

        _shot(name, width).with_suffix(".failed.png").unlink(missing_ok=True)
        if refused := self.refused(name, width):
            return self.shoot_failure(name, width, refused)
        self.draw_as_asked(name, width)
        self.screenshot(name, width)
        if _usage("usage_columns"):
            self.measure_columns(name, width)
        if self.script:
            self.evaluate_script(name, width)
        if not rule:
            return []
        self.width = width
        try:
            getattr(self, rule)(name)
        except AssertionError as found:
            return [line.strip() for line in str(found).splitlines() if line.strip()]
        except BrowserError as broke:
            return self.shoot_failure(name, width, f"{rule} could not run ({str(broke).splitlines()[0]})")
        return []

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
            # Only the toggles a reader can press: one a row does not offer is
            # drawn, but not shown. A fold first, so the rows it holds show
            # their own toggles before those are pressed.
            for toggles in ("main button[data-fold-toggle]", "main button[data-row-toggle]:not([data-fold-toggle])"):
                self.page.evaluate(
                    "(toggles) => document.querySelectorAll(toggles)"
                    ".forEach((toggle) => toggle.checkVisibility() && toggle.click())",
                    toggles,
                )

    def open_everything(self, name, width):
        """Every disclosure and popover open, round after round until none is
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
        return _beside(_usage("usage_base"), said, self.base_measured.get((name, width), {}).get(kind))

    def measure_columns(self, name, width):
        """Every table's column widths here, printed and kept beside the
        screenshot (``.columns.txt``): why a column is starved or a table
        overflows, in numbers."""

        said = "\n".join(_columns(name, width, table) for table in self.page.evaluate(_COLUMNS))
        said = said or f"{name} at {width}: (no tables)"
        shot = _shot(name, width)
        shot.with_name(shot.stem + ".columns.txt").write_text(said + "\n", encoding="utf-8")
        self.measured.setdefault((name, width), {})["columns"] = said
        print(self.beside_base(name, width, "columns", said), flush=True)

    def evaluate_script(self, name, width):
        """What ``--eval``'s script returns here, printed and kept beside the
        screenshot (``.eval.txt``): a computed style or an ancestor chain read
        in one command. One that throws says so and the look goes on."""

        try:
            said = _evaluated(name, width, self.page.evaluate(self.script))
        except BrowserError as threw:
            said = f"{name} at {width}: --eval threw ({str(threw).splitlines()[0]})"
        shot = _shot(name, width)
        shot.with_name(shot.stem + ".eval.txt").write_text(said + "\n", encoding="utf-8")
        self.measured.setdefault((name, width), {})["eval"] = said
        print(self.beside_base(name, width, "eval", said), flush=True)

    def tables(self, name):
        """What each table shows here, printed and kept beside the screenshot
        (``.tables.txt``), so two runs can be compared line by line. Not a
        check: it fails nothing."""

        said = f"{name} at {self.width}{' expanded' if _usage('usage_expand') else ''}\n"
        said += self.page.evaluate(_TABLES) or "(no tables)"
        suffix = "-expanded.tables.txt" if _usage("usage_expand") else ".tables.txt"
        shot = _shot(name, self.width)
        shot.with_name(shot.stem + suffix).write_text(said + "\n", encoding="utf-8")
        print(said, flush=True)

    def floors(self, name):
        """Each table column's derived floor beside its min-content, at each
        width a column leaves at (`browser_floors`)."""

        said, wrong = judged(measured(self.page))
        said = "\n".join([f"{name} at {self.width}", *(said or ["(no tables)"])])
        _shot(name, self.width).with_suffix(".floors.txt").write_text(said + "\n", encoding="utf-8")
        print(said, flush=True)
        if wrong:
            raise AssertionError("\n".join(wrong))

    def survey(self, name):
        """Every check the layout gates hold, on any page at any width."""

        path = self.addresses.get(name, name)
        if not getattr(self, "said_checks", False):
            self.said_checks = True
            print(f"survey checks: {', '.join(survey_checks(path, self.width))}", flush=True)
        failed = self.findings(path, self.width)
        if failed:
            raise AssertionError("\n".join(f"{check}: {line}" for check, said in failed.items() for line in said))
