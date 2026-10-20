"""A page as the browser gates draw it, for a person or an agent to look at.

`mise run dev:look -- <pages>` opens pages at a few widths, saves a screenshot
of each under var/look/, and, given `--rule`, runs that rule of
`LayoutBrowserTests` there and prints what it finds. `--css` adds a stylesheet
first: how a new rule is shown to fail on the defect it was written for before
it lands. Not a gate: it fails only when the rule it was asked to run fails.

A page is one of the stress estate's (sparse and dense), the API reference
(`api/docs`, `api/docs/dark`), or a page the example estate's navigation
reaches, named for its address under `estate` (`estate`, `estate/finance`).
`--deep` adds every page those link to: each list's records, the command forms
they open. Only the sources the pages asked for are rendered.

The example estate is answered as the browser asks, by HQ's own views over the
run's throwaway test database, each request marked as a prefetch: a page
that would act or reach out when fetched is refused there
(`hq.platform.core.speculation`), and is reported rather than looked at.

`--rule survey` runs every check the example estate's gate holds, and the ones
below that a person would otherwise have to find by eye. `--rule tables`
records what each table shows (its columns, the rows that open, the gutter,
the sort controls) beside each screenshot, for comparing two runs.
"""

import os
import re
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connections
from django.test import Client, override_settings
lazy from playwright.sync_api import Error as BrowserError

from hq.platform.core.browser_tests import (
    _DESCRIBE,
    _PAST_THE_GUTTER,
    ALL_PAGES,
    PHONE_SCREEN,
    REFERENCE,
    REFERENCE_DARK,
    BrowserGate,
    LayoutBrowserTests,
    render_reference,
    wait_for_reference,
)
from hq.platform.core.estate_browser_tests import ExampleEstateBrowserTests, navigation
from hq.platform.core.speculation import NOT_A_NAVIGATION
lazy from hq.platform.core.example_estate import REQUEST, SSO, as_production, build_showcase

OUT = Path(settings.BASE_DIR) / "var" / "look"
REFERENCE_PAGES = (REFERENCE, REFERENCE_DARK)
ESTATE = "estate"
# Request headers the browser sends that are not the view's to read.
_TRANSPORT = frozenset({"host", "cookie", "content-length", "connection"})

# The links a page offers a reader to go to: same origin, a page rather than a
# file, a dialog or a region answered in place, as speculation judges them.
_LINKS = """(notANavigation) => [...document.querySelectorAll('main a[href]')]
  .filter((a) => a.origin === location.origin && !a.matches(notANavigation))
  .map((a) => a.pathname + a.search)"""

# A word of running text broken across lines. An identifier is held to its
# own rule (`a_token_breaks_only_where_it_may`); a word never breaks.
_BROKEN_WORDS_IN_TEXT = (
    "(identifier) => {"
    + _DESCRIBE
    + r"""
  const main = document.querySelector('main');
  const range = document.createRange();
  const found = [];
  const words = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  for (let node = words.nextNode(); node; node = words.nextNode()) {
    const el = node.parentElement;
    if (el.closest(identifier) || el.closest('pre, script, style, template') || !el.checkVisibility()) continue;
    for (const word of node.data.matchAll(/[A-Za-z]{4,}/g)) {
      range.setStart(node, word.index);
      range.setEnd(node, word.index + word[0].length);
      const lines = new Set([...range.getClientRects()].filter((r) => r.width).map((r) => Math.round(r.top)));
      if (lines.size > 1) found.push(`${describe(el)} breaks the word "${word[0]}"`);
    }
  }
  return [...new Set(found)].slice(0, 15);
}"""
)

# Two lines of text drawn over each other.
_OVERLAPPING_TEXT = (
    "() => {"
    + _DESCRIBE
    + """
  const rects = [];
  const range = document.createRange();
  const walker = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.data.trim() || !node.parentElement.checkVisibility()) continue;
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) if (r.width > 2 && r.height > 2) rects.push([node, r]);
  }
  rects.sort((a, b) => a[1].top - b[1].top);
  const found = [];
  for (let i = 0; i < rects.length; i++) {
    const [one, a] = rects[i];
    for (let j = i + 1; j < rects.length && rects[j][1].top < a.bottom - 2; j++) {
      const [other, b] = rects[j];
      if (one === other) continue;
      const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (x > 2 && y > 2) {
        found.push(`"${one.data.trim().slice(0, 30)}" (${describe(one.parentElement)}) over "${other.data.trim().slice(0, 30)}" (${describe(other.parentElement)})`);
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""
)

# A column no row has anything in.
_EMPTY_COLUMNS = (
    "() => {"
    + _DESCRIBE
    + """
  const found = [];
  const shown = (cell) => cell.textContent.trim()
    || [...cell.querySelectorAll('img, svg, button, input, select, a')].some((el) => el.checkVisibility());
  for (const table of document.querySelectorAll('main table')) {
    if (!table.checkVisibility()) continue;
    const rows = [...table.querySelectorAll(':scope > tbody > tr:not([data-row-detail])')]
      .filter((row) => row.checkVisibility());
    if (!rows.length) continue;
    const width = Math.max(...rows.map((row) => row.cells.length));
    for (let column = 0; column < width; column++) {
      const cells = rows.map((row) => row.cells[column]).filter((cell) => cell && cell.colSpan === 1 && cell.checkVisibility());
      if (cells.length === rows.length && !cells.some(shown)) {
        const head = table.tHead?.rows[0]?.cells[column];
        found.push(`${describe(table)} column ${column + 1} ("${head ? head.textContent.trim() : ''}") is empty in every row`);
      }
    }
  }
  return found;
}"""
)

# What is an identifier, held to its own rule rather than a word's.
_IDENTIFIER = "code, kbd, samp, time, .token, [data-entity]"

# Each table as a reader finds it: which columns show, which are gone and
# whether for being blank or for want of room, which rows offer to open and
# have the gutter for it, the sort controls, and what each open row lists.
_TABLES = r"""() => {
  const name = (el) => {
    if (!el) return '';
    const copy = el.cloneNode(true);
    copy.querySelectorAll('[aria-hidden=true]').forEach((mark) => mark.remove());
    return copy.textContent.trim().replace(/\s+/g, ' ');
  };
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


def _usage(variable: str) -> str:
    """What the task was given: mise exports each argument and flag of a
    task's usage as ``usage_<name>``, lower case, and an unset flag as nothing
    at all."""

    return os.environ.get(variable, "")


def _listed(name: str) -> list[str]:
    return [part.strip() for part in _usage(name).split(",") if part.strip()]


def _estate_name(path: str) -> str:
    return "/".join(filter(None, (ESTATE, path.strip("/"))))


def _is_estate(name: str) -> bool:
    return name == ESTATE or name.startswith(f"{ESTATE}/")


def _estate_path(name: str) -> str:
    rest = name.removeprefix(ESTATE).strip("/")
    return f"/{rest}/" if rest else "/"


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
    def render(cls):
        estate = [_estate_name(path) for path in navigation()]
        known = [*ALL_PAGES, *REFERENCE_PAGES, *estate]
        pages = _listed("usage_pages")
        if pages == ["all"]:
            pages = known
        elif pages == [f"{ESTATE}/all"]:
            pages = estate
        unknown = sorted(set(pages) - set(known))
        if unknown:
            raise AssertionError(f"No page {unknown}. Pages: {', '.join(known)}")
        cls.asked = pages
        cls.addresses = {name: _estate_path(name) for name in estate}
        cls.estate_client = cls.serve_estate() if set(pages) & set(estate) else None
        rendered: dict = {}
        if set(pages) & set(ALL_PAGES):
            rendered |= LayoutBrowserTests.render()
        if set(pages) & set(REFERENCE_PAGES):
            rendered |= render_reference()
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
            or parts.path.strip("/") in self.pages
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

    def open(self, name, width):
        if _is_estate(name):
            self.visit(self.addresses[name], width)
            return
        super().open(name, width)
        if name in REFERENCE_PAGES and not _usage("usage_no_script"):
            wait_for_reference(self.page)

    def test_look(self):
        widths, rule = self.asked_for()
        # A page's own policy refuses a stylesheet it did not name; `--css` is one.
        self.start(java_script_enabled=not _usage("usage_no_script"), bypass_csp=bool(_usage("usage_css")))
        if _usage("usage_dark"):
            self.page.emulate_media(color_scheme="dark")
        OUT.mkdir(parents=True, exist_ok=True)
        if _usage("usage_deep"):
            self.follow(widths[0])
        failed = []
        for name in self.asked:
            for width in widths:
                found = self.look(name, width, rule)
                report = _shot(name, width).with_suffix(".txt")
                if found:
                    failed.append(found)
                    report.write_text(found + "\n", encoding="utf-8")
                    print(found, flush=True)
                else:
                    report.unlink(missing_ok=True)
        if rule:
            self.assertEqual(failed, [], f"{rule} does not hold")
            print(f"{rule} holds on every page at {', '.join(map(str, widths))}")

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
        """The widths and rule the task was given, each checked."""

        widths = [int(width) for width in _listed("usage_widths")] or [PHONE_SCREEN[0], 820, 1360]
        rule = _usage("usage_rule")
        self.assertTrue(not rule or callable(getattr(self, rule, None)), f"No rule named {rule!r}")
        return widths, rule

    def look(self, name, width, rule):
        """Screenshot ``name`` at ``width``, and what ``rule`` finds there."""

        try:
            self.open(name, width)
        except BrowserError as refused:
            return f"{name} at {width}: not a page ({str(refused).splitlines()[0]})"
        if _is_estate(name) and (said := self.answered.get(_address(self.page.url))) != (200, ""):
            return f"{name}: answered {said}, not looked at"
        if not self.page.locator("main").count():
            return f"{name}: not a page of HQ's (no main), not looked at"
        if css := _usage("usage_css"):
            self.page.add_style_tag(content=css)
        if _usage("usage_open"):
            self.open_every_disclosure(name)
        if _usage("usage_expand"):
            # Only the toggles a reader can press: one a row does not offer is
            # drawn, but not shown.
            self.page.evaluate(
                "() => document.querySelectorAll('main button[data-row-toggle]')"
                ".forEach((toggle) => toggle.checkVisibility() && toggle.click())"
            )
        shot = _shot(name, width)
        self.page.screenshot(path=shot, full_page=True)
        print(shot, flush=True)
        if not rule:
            return None
        self.width = width
        try:
            getattr(self, rule)(name)
        except AssertionError as found:
            return f"{name} at {width}: {found}"
        except BrowserError as broke:
            return f"{name} at {width}: {rule} could not run ({str(broke).splitlines()[0]})"
        return None

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

    def survey(self, name):
        """Every check the example estate's gate holds, and the ones a person
        would otherwise find only by looking."""

        found = {
            **self.geometry(self.addresses.get(name, name), self.width),
            "words broken in text": self.page.evaluate(_BROKEN_WORDS_IN_TEXT, _IDENTIFIER),
            "past the gutter": self.page.evaluate(_PAST_THE_GUTTER, ["*", True]),
            "overlapping text": self.page.evaluate(_OVERLAPPING_TEXT),
            "empty columns": self.page.evaluate(_EMPTY_COLUMNS),
            "frames": self.palette()["frames"],
        }
        failed = {check: said for check, said in found.items() if said}
        if failed:
            raise AssertionError(
                "\n" + "\n".join(f"  {check}: {line}" for check, said in failed.items() for line in said)
            )
