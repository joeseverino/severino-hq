"""The layout gate over the example estate: every page the navigation reaches,
and a record's page of each kind it lists.

``layout.LayoutBrowserTests`` holds its invariants over the stress
estate, which is built to break layouts and covers the pages it was built for.
This holds the same invariants over the homelab the demo shows
(``hq/platform/core/example_estate``), on every page a person reaches from the
navigation, as a reader sees it: script on, deferred parts loaded, at a phone,
a tablet, a laptop and a wide desktop, in the light palette and the dark.

Each page is rendered once, opened once per width, and every check runs on
that one load; a finding names its check, page, width and theme.
"""

import json
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import cache
from html import unescape
from types import MappingProxyType
from typing import Any, override
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

from django.conf import settings
from django.test import Client
from django.urls import reverse
lazy from django.db import transaction

from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.in_flight import InFlight
from hq.platform.core.tests.browser.probes.boxes import SIDEWAYS
from hq.platform.core.tests.browser.probes.describe import TEXT_SHOWN
from hq.platform.core.tests.browser.probes.settle import FINISH_TRANSITIONS, FONTS_READY, SETTLE
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.survey import geometry_probes
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE, WIDE_TABLES, WIDTHS, viewport
lazy from hq.platform.application import fragments
lazy from hq.platform.application.domains import domain_navigation
lazy from hq.platform.core import seeds
lazy from hq.platform.core.example_estate import REQUEST
lazy from hq.platform.core.example_estate.names import MACHINE, SERVICES
lazy from hq.platform.core.example_estate.network import ZONE_KEY
lazy from hq.platform.core.example_estate.testing import as_production, sign_in

# The gate's widths, a wide desktop and the laptop windows where a wide table
# has the least room: the example estate is the homelab as a person reads it,
# on every screen up to the widest.
READ_AT = tuple(sorted((*WIDTHS, 1680, *WIDE_TABLES)))
THEMES = ("light", "dark")
# The API reference mounts a vendored viewer, whose markup is not HQ's to hold
# to these rules; ReferenceBrowserTests holds it to its own.
_NOT_HQS_MARKUP = {"api_reference:reference"}
# Reached from the header on every page rather than from the navigation.
_HEADER = ("action_items",)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# Each popover's kind: its classes and the table and column it sits in. Popovers
# of one kind are drawn by one partial in one place, so one of them stands for
# all, and a pass over them costs a run per kind rather than per record.
POPOVER_KINDS = """() => {
  const tables = [...document.querySelectorAll('table')];
  return [...document.querySelectorAll('[popover]')].map((popover) => {
    const table = popover.closest('table');
    return [popover.className, tables.indexOf(table), popover.closest('td, th')?.cellIndex ?? -1].join(' ');
  });
}"""
# The popover at a place in the page shown, what names it in a finding and
# what names its shot; nothing for one that will not show.
SHOW_POPOVER = """(place) => {
  const popover = document.querySelectorAll('[popover]')[place];
  try {
    popover.showPopover();
  } catch {
    return null;
  }
  return popover.id ? [`#${popover.id}`, popover.id] : [`popover ${place + 1}`, `${place + 1}`];
}"""
HIDE_POPOVER = "(place) => document.querySelectorAll('[popover]')[place].hidePopover()"
# The checks a shown popover's own boxes and lines are held to: its rows are
# laid out by the same primitives as the page's, over whatever lies beneath.
_IN_A_POPOVER = ("overlaps", "overlapping text")


def fragments_of(client, address, page, answers, ask):
    """Every part the page at ``address`` fetches into itself, and the parts
    inside those, by address and part name, as the server answers them."""

    pending = [(address, page)]
    while pending:
        at, body = pending.pop()
        for url, name in fragments.regions(body, at):
            if (url, name) in answers:
                continue
            headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest", **({"HTTP_X_FRAGMENT": name} if name else {})}
            response = ask(client, url, **headers)
            if response.status_code == 200:
                answers[url, name] = answered(response)
                pending.append((url, answers[url, name]))


def navigation():
    """Every page the navigation declares, and the header's, by address."""

    routes = [entry.route for entry in domain_navigation() if entry.route not in _NOT_HQS_MARKUP]
    return list(dict.fromkeys(reverse(route) for route in (*routes, *_HEADER)))


class Redirected(Exception):
    """A page that leaves HQ or redirects back to itself, so it lands on no page of HQ's."""


def landing(ask: Callable[[str], Any], address: str) -> str:
    """The page ``address`` lands on: each redirect within HQ followed to the page that answers.

    A navigation entry may be a stop on the way (Domains goes straight to a domain), and a list
    of pages names the page a reader reaches, which is the one a gate draws and perf measures."""

    seen = [address]
    while (response := ask(address)).status_code in _REDIRECTS:
        to = urlsplit(urljoin(address, response.headers["Location"]))
        if to.scheme or to.netloc:
            raise Redirected(f"{seen[0]} redirects out of HQ, to {to.geturl()}")
        address = to.path + (f"?{to.query}" if to.query else "")
        if address in seen:
            raise Redirected(f"{seen[0]} redirects in a loop: {' -> '.join([*seen, address])}")
        seen.append(address)
    return address


def landings(ask: Callable[[str], Any], addresses: Sequence[str]) -> list[str]:
    """Each of ``addresses`` as the page it lands on, once each, in order."""

    return list(dict.fromkeys(landing(ask, address) for address in addresses))


def ask_as_production(client, url, **headers):
    """``url`` asked of HQ the way production asks it, its redirect answered rather than followed."""

    return client.get(url, secure=True, **REQUEST, **headers)


@contextmanager
def owner_client(seed: str = "showcase") -> Iterator[Client]:
    """A client signed in as the owner of ``seed`` built in this database, answered as production
    answers, everything it built rolled back when the block ends."""

    with transaction.atomic(), as_production(), seeds.built(seed) as owner:
        client = Client()
        sign_in(client, owner)
        yield client
        transaction.set_rollback(True)


def records():
    """A record's page of each kind the navigation lists: the machine, the
    service with the longest name, its container (which has an upgrade to
    make), the zone, and a message with the page that deletes it."""

    hostname, container, *_ = max(SERVICES, key=lambda service: len(service[0]))
    return [
        reverse("control_plane:machine", kwargs={"name": MACHINE}),
        reverse("control_plane:service", kwargs={"hostname": hostname}),
        reverse("control_plane:detail", kwargs={"key": f"{container}-container"}),
        reverse("control_plane:detail", kwargs={"key": ZONE_KEY}),
        reverse("contacts:detail", args=[1]),
        reverse("contacts:delete", args=[1]),
    ]


def render_estate():
    """The navigation's pages, and every page and part the browser will ask
    for, rendered for the estate's owner the way production asks:
    ``(paths, {(address, part): body})``."""

    def ask(client, url, **headers):
        return client.get(url, secure=True, follow=True, **REQUEST, **headers)

    answers = {}
    with owner_client() as client:
        paths = landings(lambda address: ask_as_production(client, address), [*navigation(), *records()])
        for path in paths:
            # Asked as listed, unfollowed: a page in the list is the page itself, never a redirect to one.
            response = ask_as_production(client, path)
            if response.status_code != 200:
                raise AssertionError(f"{path} answered {response.status_code}")
            answers[path, ""] = answered(response)
            fragments_of(client, path, answers[path, ""], answers, ask)
    return paths, answers


@cache
def estate_once():
    """``render_estate`` once a process, so a worker handed both of the
    estate's sweeps builds it once; it is rolled back once rendered."""

    paths, answers = render_estate()
    return tuple(paths), MappingProxyType(answers)


class ExampleEstateGate(BrowserGate):
    """The example estate's pages in a browser, read as a person reads them:
    script on, and every part a page fetches answered. Its sweeps are the
    classes below, so two workers run them side by side."""

    scripted = True
    navigation: tuple[str, ...]

    @classmethod
    @override
    def render(cls):
        cls.navigation, answers = estate_once()
        return answers

    @override
    def start(self, **options):
        super().start(**options)
        self.unanswered = []
        # `answer` answers every request, so the count comes back to zero.
        self.in_flight = InFlight(self.page)

    @override
    def answer(self, route):
        request = route.request
        parts = urlsplit(request.url)
        address = parts.path + (f"?{parts.query}" if parts.query else "")
        part = request.headers.get("x-fragment", "")
        if request.method != "GET":
            # What a page asks for by itself once open (a record's page asks
            # for a fresh read) acts, and the gate only reads.
            route.fulfill(status=405, body="The layout gate only reads")
        elif (address, part) in self.pages:
            self.fulfill(route, self.pages[address, part])
        elif parts.path.startswith(settings.STATIC_URL):
            super().answer(route)
        else:
            # The estate holds each (address, part) fragments.regions found on a
            # page it rendered, so a miss means it never found that region there.
            self.unanswered.append(
                f"{address} {part}".strip() + ": never pre-rendered; fragments.regions found no such region"
            )
            route.fulfill(status=404, body="Not in the example estate")

    def each_popover_kind(self):
        """Each kind of popover on the page shown on its own in turn, as a
        reader shows one: what names it in a finding and in a shot, and how
        many more of its kind it stands for. Shown all at once they would lie
        over one another, which no reader sees, and where each lands would
        hang on which drew last."""

        kinds = self.page.evaluate(POPOVER_KINDS)
        judged = set()
        for place, kind in enumerate(kinds):
            if kind in judged or (shown := self.page.evaluate(SHOW_POPOVER, place)) is None:
                continue
            judged.add(kind)
            try:
                yield shown, kinds.count(kind) - 1
            finally:
                self.page.evaluate(HIDE_POPOVER, place)

    def visit(self, path, width):
        self.page.set_viewport_size(viewport(width))
        self.page.goto(f"{ORIGIN}{path}", wait_until="load")
        self.in_flight.settle(f"{path} at {width}px")
        self.page.evaluate(FONTS_READY)


class ExampleEstateBrowserTests(ExampleEstateGate):
    """Every navigation page of the example estate keeps the layout rules."""

    def opened(self, path, width):
        """What holds once every disclosure on the page is open, and inside
        each kind of popover shown."""

        self.page.evaluate(
            "(disclosure) => document.querySelectorAll(disclosure).forEach((d) => { d.open = true; })",
            SELECTORS["disclosure"],
        )
        self.page.evaluate(SETTLE)
        found = {"sideways, opened": self.page.evaluate(SIDEWAYS, SELECTORS["sideways"])}
        probes = {check: probe for check, probe in geometry_probes(path, width).items() if check in _IN_A_POPOVER}
        for (label, _slug), _alike in self.each_popover_kind():
            self.page.evaluate(SETTLE)
            found |= {f"{check}, with {label} shown": said for check, said in self.checked(probes).items()}
        return found

    def test_every_navigation_page_keeps_the_layout_rules(self):
        self.assertGreater(len(self.navigation), 10, "the navigation led almost nowhere")
        for path in self.navigation:
            for width in READ_AT:
                with self.subTest(page=path, width=width):
                    self.page.emulate_media(color_scheme="light")
                    self.visit(path, width)
                    found = self.geometry(path, width)
                    for theme in THEMES:
                        self.page.emulate_media(color_scheme=theme)
                        self.page.evaluate(SETTLE)
                        self.page.evaluate(FINISH_TRANSITIONS)
                        found |= {f"{check} ({theme})": said for check, said in self.palette().items()}
                    found |= self.opened(path, width)
                    found["unanswered"], self.unanswered = self.unanswered, []
                    found["refused"] = self.refusals_so_far()
                    failed = {check: said for check, said in found.items() if said}
                    if failed:
                        self.fail(json.dumps(failed, indent=1))


# A list's toolbar boxes (a filter's options and its toggles), the sort its
# server stands at, the links its headings sort by, and the queue's choices.
_TOOLBAR_BOX = re.compile(r'<input type="checkbox" name="([^"]+)" value="([^"]*)"')
_DEFAULT_SORT = re.compile(r'<select name="sort">.*?<option value="([^"]*)" selected>', re.DOTALL)
_SORT_LINK = re.compile(r'class="table-sort-link" href="([^"]+)"')
_QUEUE_CHOICES = re.compile(r'<select name="(status|source)"[^>]*>(.*?)</select>', re.DOTALL)
_OPTION = re.compile(r'<option value="([^"]+)"')
_FIRST_ROW = re.compile(r"<tbody>\s*<tr[^>]*>(.*?)</tr>", re.DOTALL)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)


def _cell_words(row):
    return (re.sub(r"<[^>]+>", " ", cell).split() for cell in _CELL.findall(row))


# What no record is called, so a search for it empties the list.
_NOTHING = "nothing-is-called-this"
# A phone and a laptop: where a control moves into a menu, and where it does not.
PRESSED_AT = (PHONE_SCREEN[0], WIDE)
# The address shows what was asked for and the answer is in place.
_ASKED = """([name, value]) => new URLSearchParams(location.search).getAll(name).includes(value)
  && !document.querySelector('main[aria-busy]')"""
_LET_GO = """([name, value]) => !new URLSearchParams(location.search).getAll(name).includes(value)
  && !document.querySelector('main[aria-busy]')"""
# The places of the headings a list's table has dropped, and those places as
# each frame draws them from now on.
_DROPPED = """(region) => [...document.querySelector(region + ' > .data-table').tHead.rows[0].cells]
  .flatMap((heading, place) => heading.hasAttribute('data-dropped') ? [place] : [])"""
_WATCH_DROPPED = (
    """(region) => {
  window.droppedFrames = [];
  const look = () => {
    window.droppedFrames.push(("""
    + _DROPPED
    + """)(region));
    requestAnimationFrame(look);
  };
  requestAnimationFrame(look);
}"""
)
_FIRST_COLUMN = """(rows) => [...document.querySelectorAll(rows)]
  .map((row) => row.cells[0].innerText.trim().split('\\n')[0])"""
# A word in a closed row's line beneath that nothing else find could reach
# holds: no text drawn on the page, and no other line beneath a row.
_HELD_BACK_WORD = (
    "(details) => {"
    + TEXT_SHOWN
    + """
  const lines = [...document.querySelectorAll(details)];
  const reachable = (node) => textShown(node) || lines.some((line) => line.contains(node));
  const texts = (root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const found = [];
    while (walker.nextNode()) found.push(walker.currentNode);
    return found;
  };
  for (const line of lines) {
    if (line.previousElementSibling?.classList.contains('is-expanded')) continue;
    const shown = (node) => {
      for (let el = node.parentElement; el && el !== line; el = el.parentElement) {
        if (getComputedStyle(el).display === 'none') return false;
      }
      return true;
    };
    const elsewhere = texts(document.body)
      .filter((node) => !line.contains(node) && reachable(node))
      .map((node) => node.data.toLowerCase()).join(' ');
    for (const node of texts(line).filter(shown)) {
      for (const word of node.data.split(/[^A-Za-z]+/)) {
        if (word.length > 4 && !elsewhere.includes(word.toLowerCase())) return word;
      }
    }
  }
  return null;
}"""
)
# How tall each table holding a closed row's line is drawn, then with every
# such line left out of the page.
_CLOSED_LINES_ROOM = """(details) => {
  const lines = [...document.querySelectorAll(details)]
    .filter((line) => !line.previousElementSibling.classList.contains('is-expanded'));
  const tables = [...new Set(lines.map((line) => line.closest('table')))];
  const heights = () => tables.map((table) => table.getBoundingClientRect().height);
  const drawn = heights();
  lines.forEach((line) => line.style.setProperty('display', 'none'));
  const without = heights();
  lines.forEach((line) => line.style.removeProperty('display'));
  return [drawn, without];
}"""
# The line holding the word is open beneath its row.
_FOUND_OPEN = """([details, word]) => [...document.querySelectorAll(details)].some((line) =>
  line.textContent.includes(word) && line.checkVisibility()
  && line.previousElementSibling.classList.contains('is-expanded'))"""


class ExampleEstateControlTests(BrowserGate):
    """Every kind of control on a list page does what it says when pressed, on
    the example estate at a phone's width and a laptop's: a filter, a search, a
    heading that sorts, the queue's own filter and the menus that hold them.
    The answers are rendered first, every address a press can ask for, keyed by
    what it asks (a blank field and the sort a list stands at by default ask
    nothing)."""

    scripted = True
    # A list's default sort, and the first word of its first row, by path.
    default_sort: dict[str, str]
    word: dict[str, str]

    @classmethod
    @override
    def render(cls):
        cls.default_sort, cls.word = {}, {}
        answers = {}
        with transaction.atomic(), as_production(), seeds.built("showcase") as owner:
            client = Client()
            sign_in(client, owner)

            def ask(address):
                response = client.get(address, secure=True, follow=True, **REQUEST)
                if response.status_code != 200:
                    raise AssertionError(f"{address} answered {response.status_code}")
                body = answered(response)
                path = urlsplit(address).path
                if path not in cls.default_sort:
                    default = _DEFAULT_SORT.search(body)
                    cls.default_sort[path] = unescape(default.group(1)) if default else ""
                answers[cls.key(address)] = body
                return body

            for path in (reverse("expenses:list"), reverse("projects:list")):
                cls.ask_of_a_list(ask, path)
            queue = reverse("action_items")
            for name, options in _QUEUE_CHOICES.findall(ask(queue)):
                for value in _OPTION.findall(options):
                    ask(f"{queue}?{name}={value}")
            transaction.set_rollback(True)
        return answers

    @classmethod
    def ask_of_a_list(cls, ask, path):
        """Every address a list's controls can ask for: each box ticked alone,
        each heading's sort both ways, a search for its first row's first word
        and one for nothing."""

        body = ask(path)
        for name, value in _TOOLBAR_BOX.findall(body):
            ask(f"{path}?{name}={value}")
        for href in _SORT_LINK.findall(body):
            for deeper in _SORT_LINK.findall(ask(urljoin(path, unescape(href)))):
                ask(urljoin(path, unescape(deeper)))
        first = _FIRST_ROW.search(body)
        if first is None:
            raise AssertionError(f"{path} lists no row to search for")
        row = first.group(1)
        cls.word[path] = next(unescape(words[0]) for words in _cell_words(row) if words)
        ask(f"{path}?q={cls.word[path]}")
        ask(f"{path}?q={_NOTHING}")

    @classmethod
    def key(cls, address):
        """What an address asks: its path and the fields it fills, less the
        sorts a list takes when none is named (its default, and relevance,
        which a search's toolbar names once it has a query)."""

        parts = urlsplit(address)
        unnamed = {cls.default_sort.get(parts.path), "_relevance"}
        asked = frozenset(
            (name, value)
            for name, value in parse_qsl(parts.query)
            if value and not (name == "sort" and value in unnamed)
        )
        return parts.path, asked

    @override
    def answer(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method != "GET":
            route.fulfill(status=405, body="The control gate only reads")
        elif self.key(request.url) in self.pages:
            self.fulfill(route, self.pages[self.key(request.url)])
        elif path.startswith(settings.STATIC_URL):
            super().answer(route)
        else:
            # A part the page asks for by itself is not what these hold.
            route.fulfill(status=404, body="Not pressed here")

    def visit(self, path, width):
        self.page.set_viewport_size(viewport(width))
        self.page.goto(f"{ORIGIN}{path}", wait_until="load")
        self.page.evaluate(FONTS_READY)
        self.page.evaluate(SETTLE)

    def rows(self):
        return self.page.locator(SELECTORS["table_rows"]).count()

    def test_a_filter_narrows_its_list_keeps_it_in_the_address_and_lets_it_go(self):
        path = reverse("expenses:list")
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(path, width)
                # Every answer below is drawn on this page, never by loading another.
                self.page.evaluate("() => { window.kept = true; }")
                every = self.rows()
                self.page.locator(SELECTORS["table_filter"]).first.click()
                menu = self.page.locator(SELECTORS["table_filter_panel"]).first
                box = menu.locator(SELECTORS["table_filter_box"]).first
                asked = [box.get_attribute("name"), box.get_attribute("value")]
                box.check()
                self.page.wait_for_function(_ASKED, arg=asked)
                self.assertLess(self.rows(), every)
                # The answer's menu is open where the reader left it, the box ticked.
                self.assertTrue(menu.evaluate("(menu) => menu.matches(':popover-open')"))
                self.assertTrue(box.is_checked())
                box.uncheck()
                self.page.wait_for_function(_LET_GO, arg=asked)
                self.assertEqual(self.rows(), every)

                # A list its filters empty says so, and gives every row back.
                toggle = self.page.locator(SELECTORS["table_toggle"]).first
                asked = [toggle.get_attribute("name"), toggle.get_attribute("value")]
                toggle.check()
                self.page.wait_for_function(_ASKED, arg=asked)
                self.assertEqual(self.rows(), 0)
                self.assertTrue(self.page.locator(SELECTORS["table_empty"]).is_visible())
                toggle.uncheck()
                self.page.wait_for_function(_LET_GO, arg=asked)
                self.assertEqual(self.rows(), every)
                self.assertFalse(self.page.locator(SELECTORS["table_empty"]).count())

                # Back is the list as it was, the address and the box agreeing,
                # and Forward the list after it.
                self.page.go_back()
                self.page.wait_for_function(_ASKED, arg=asked)
                self.assertEqual(self.rows(), 0)
                self.assertTrue(toggle.is_checked())
                self.page.go_forward()
                self.page.wait_for_function(_LET_GO, arg=asked)
                self.assertEqual(self.rows(), every)
                self.assertFalse(toggle.is_checked())
                self.assertTrue(self.page.evaluate("() => window.kept === true"))

    def test_a_search_narrows_its_list_and_clearing_it_brings_every_row_back(self):
        path = reverse("projects:list")
        word = self.word[path]
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(path, width)
                self.page.evaluate("() => { window.kept = true; }")
                every = self.rows()
                search = self.page.locator(SELECTORS["table_search"])
                search.fill(word)
                self.page.wait_for_function(_ASKED, arg=["q", word])
                found = self.page.locator(SELECTORS["table_rows"]).all_inner_texts()
                self.assertGreater(len(found), 0)
                self.assertLess(len(found), every)
                self.assertEqual([row for row in found if word.lower() not in row.lower()], [])
                # Typing carries on in the box: the answer did not take it away.
                self.assertTrue(search.evaluate("(box) => box === document.activeElement"))
                search.fill(_NOTHING)
                self.page.wait_for_function(_ASKED, arg=["q", _NOTHING])
                self.assertEqual(self.rows(), 0)
                self.assertTrue(self.page.locator(SELECTORS["table_empty"]).is_visible())
                search.fill("")
                self.page.wait_for_function(_LET_GO, arg=["q", _NOTHING])
                self.assertEqual(self.rows(), every)
                self.assertTrue(self.page.evaluate("() => window.kept === true"), "a search loaded another page")

    def test_a_list_sorted_in_place_still_fits_a_window_that_narrows(self):
        """A sort swaps the table in to be laid out around its own rows, so a
        narrower window drops a column rather than holding the table at its
        old width past its region."""

        self.visit(reverse("projects:list"), 1360)
        link = self.page.locator(SELECTORS["sort_link"]).first
        asked = dict(parse_qsl(urlsplit(link.get_attribute("href")).query))["sort"]
        link.click()
        self.page.wait_for_function(_ASKED, arg=["sort", asked])
        self.page.set_viewport_size({"width": 400, "height": PHONE_SCREEN[1]})
        fits = """(region) => {
          const table = document.querySelector(region + ' > table');
          return table.offsetWidth <= table.parentElement.clientWidth
            && [...table.tHead.rows[0].cells].some((heading) => heading.hasAttribute('data-dropped'));
        }"""
        self.page.wait_for_function(fits, arg=SELECTORS["table_scroll"], timeout=5_000)

    def test_a_list_sorted_on_a_phone_draws_no_column_it_had_dropped(self):
        """The table a sort swaps in is fitted before its first frame, so no
        column the list had dropped is drawn and then dropped again."""

        self.visit(reverse("projects:list"), PHONE_SCREEN[0])
        before = self.page.evaluate(_DROPPED, SELECTORS["table_scroll"])
        self.assertTrue(before, "a phone's list dropped no column, so the sort has nothing to keep")
        self.page.evaluate(_WATCH_DROPPED, SELECTORS["table_scroll"])
        link = self.page.locator(SELECTORS["sort_link"]).first
        asked = dict(parse_qsl(urlsplit(link.get_attribute("href")).query))["sort"]
        link.click()
        self.page.wait_for_function(_ASKED, arg=["sort", asked])
        self.page.evaluate(SETTLE)
        frames = self.page.evaluate("() => window.droppedFrames")
        self.assertTrue(frames, "no frame was drawn")
        self.assertEqual([frame for frame in frames if frame != before], [])

    def test_a_sort_heading_orders_its_list_and_says_which_way(self):
        path = reverse("projects:list")
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(path, width)
                self.page.evaluate("() => { window.kept = true; }")
                link = self.page.locator(SELECTORS["sort_link"]).first
                sorted_by = "(link) => link.closest('th').getAttribute('aria-sort')"
                self.assertEqual(link.evaluate(sorted_by), "none")
                for direction in ("ascending", "descending"):
                    asked = dict(parse_qsl(urlsplit(link.get_attribute("href")).query))["sort"]
                    link.click()
                    self.page.wait_for_function(_ASKED, arg=["sort", asked])
                    self.assertEqual(link.evaluate(sorted_by), direction)
                    names = self.page.evaluate(_FIRST_COLUMN, SELECTORS["table_rows"])
                    self.assertEqual(names, sorted(names, key=str.lower, reverse=direction == "descending"))
                self.assertTrue(self.page.evaluate("() => window.kept === true"), "a sort loaded another page")

    def test_the_queue_keeps_only_the_urgency_it_is_asked_for(self):
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(reverse("action_items"), width)
                every = self.page.locator(SELECTORS["decisions"]).count()
                urgency = self.page.locator(SELECTORS["queue_urgency"])
                # The urgency the first row is at, so the answer has rows to hold.
                offered = urgency.locator("option").evaluate_all("(options) => options.map((option) => option.value)")
                first = self.page.locator(f"{SELECTORS['decisions']} .pill").first.get_attribute("class").split()
                chosen = next(value for value in offered if value and f"pill-{value}" in first)
                urgency.select_option(chosen)
                with self.page.expect_navigation():
                    self.page.locator(SELECTORS["queue_apply"]).click()
                self.assertIn(("status", chosen), parse_qsl(urlsplit(self.page.url).query))
                self.assertEqual(urgency.input_value(), chosen)
                rows = self.page.locator(SELECTORS["decisions"])
                shown = rows.count()
                self.assertGreater(shown, 0)
                self.assertLessEqual(shown, every)
                marks = rows.locator(".pill").evaluate_all("(pills) => pills.map((pill) => [...pill.classList])")
                self.assertEqual([mark for mark in marks if f"pill-{chosen}" not in mark], [])

    def test_a_category_opened_in_the_phone_drawer_keeps_the_drawer_open(self):
        """A category's menu lives inside the drawer that holds it, so pressing
        it opens the category and leaves the drawer open around it."""

        self.visit(reverse("expenses:list"), PHONE_SCREEN[0])
        self.page.locator(SELECTORS["nav_toggle"]).click()
        drawer = self.page.locator(SELECTORS["nav_drawer"])
        self.assertTrue(drawer.is_visible())
        menu = self.page.locator(SELECTORS["nav_menu"]).first
        menu.locator("summary").click()
        self.assertTrue(menu.evaluate("(menu) => menu.open"))
        self.assertTrue(menu.locator("a").first.is_visible())
        self.assertTrue(drawer.is_visible())

    def test_the_phone_drawer_opens_closes_and_hands_focus_back(self):
        """The drawer opens from its button by a press or a key, says it is
        open, holds no focus captive, and closes on its button, Escape or a
        press outside, Escape handing focus back to the button."""

        self.visit(reverse("expenses:list"), PHONE_SCREEN[0])
        toggle = self.page.locator(SELECTORS["nav_toggle"])
        drawer = self.page.locator(SELECTORS["nav_drawer"])
        self.assertFalse(drawer.is_visible())
        toggle.click()
        self.assertTrue(drawer.is_visible())
        # The button is the drawer's invoker, which is how the browser tells
        # assistive technology the button is expanded; Playwright's role
        # locator reads only an `aria-expanded` attribute, so it is asked here.
        self.assertTrue(toggle.evaluate("(toggle) => toggle.popoverTargetElement.matches(':popover-open')"))
        toggle.click()
        self.assertFalse(drawer.is_visible())
        toggle.click()
        self.page.locator(SELECTORS["page_title"]).click()
        self.assertFalse(drawer.is_visible())

        toggle.focus()
        self.page.keyboard.press("Enter")
        self.assertTrue(drawer.is_visible())
        # Its sections follow the button in the tab order, and it is not
        # modal, so nothing outside it is made inert while it is open.
        self.page.keyboard.press("Tab")
        self.assertTrue(drawer.evaluate("(drawer) => drawer.contains(document.activeElement)"))
        self.assertFalse(drawer.evaluate("(drawer) => drawer.matches(':modal')"))
        self.page.keyboard.press("Escape")
        self.assertFalse(drawer.is_visible())
        self.assertTrue(toggle.evaluate("(toggle) => toggle === document.activeElement"))

    def test_the_phone_drawer_opens_and_closes_with_no_script(self):
        self.start(java_script_enabled=False)
        self.page.set_viewport_size(viewport(PHONE_SCREEN[0]))
        self.page.goto(f"{ORIGIN}{reverse('expenses:list')}", wait_until="load")
        toggle = self.page.locator(SELECTORS["nav_toggle"])
        drawer = self.page.locator(SELECTORS["nav_drawer"])
        self.assertFalse(drawer.is_visible())
        toggle.click()
        self.assertTrue(drawer.is_visible())
        self.assertTrue(drawer.get_by_role("link").first.is_visible())
        toggle.click()
        self.assertFalse(drawer.is_visible())

    def test_find_in_page_opens_the_closed_row_that_holds_the_match(self):
        """A word only a closed row's line beneath holds is found, as the
        browser's find finds it (a text fragment), and the row opens on it."""

        self.visit(reverse("expenses:list"), PHONE_SCREEN[0])
        word = self.page.evaluate(_HELD_BACK_WORD, SELECTORS["row_detail"])
        self.assertTrue(word, "no closed row holds back a word of its own")
        # Reachable by find, a closed row's line still takes no room. Left out,
        # a line under the last row would leave that row its rule, which the
        # last row never draws, so a table may be a rule shorter drawn.
        drawn, without = self.page.evaluate(_CLOSED_LINES_ROOM, SELECTORS["row_detail"])
        for tall, room in zip(drawn, without, strict=True):
            self.assertLessEqual(tall, room)
        self.page.goto(f"{ORIGIN}{reverse('expenses:list')}#:~:text={quote(word)}", wait_until="load")
        self.page.wait_for_function(_FOUND_OPEN, arg=[SELECTORS["row_detail"], word])

    def test_a_menu_opens_and_closes_on_escape_and_a_press_outside(self):
        # The page's clock stands still, so a press lands while the hover delay
        # its pointer started is still pending, as a quick hand's does. A
        # dismissal is read once every delay has run out: it has to hold.
        # A menu is pressed by its handle: a filter's button, whose panel is a
        # popover, or a disclosure's summary.
        def is_open(handle):
            return handle.evaluate(
                "(handle) => handle.popoverTargetElement?.matches(':popover-open') ?? handle.closest('details').open"
            )

        def every_delay_later():
            self.page.clock.run_for(1000)

        self.page.clock.install()
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.page.clock.resume()
                self.visit(reverse("expenses:list"), width)
                self.page.clock.pause_at(self.page.evaluate("() => Date.now() / 1000 + 1"))
                handles = [self.page.locator(SELECTORS["table_filter"]).first]
                if width >= WIDE:
                    handles.append(self.page.locator(SELECTORS["nav_menu"]).first.locator("summary"))
                for handle in handles:
                    handle.click()
                    self.assertTrue(is_open(handle))
                    self.page.keyboard.press("Escape")
                    every_delay_later()
                    self.assertFalse(is_open(handle))
                    handle.click()
                    self.assertTrue(is_open(handle))
                    self.page.locator(SELECTORS["page_title"]).click()
                    every_delay_later()
                    self.assertFalse(is_open(handle))
                if width < WIDE:
                    # A phone's menus are in a drawer, which Escape closes whole.
                    self.page.locator(SELECTORS["nav_toggle"]).click()
                    menu = self.page.locator(SELECTORS["nav_menu"]).first
                    menu.locator("summary").click()
                    self.assertTrue(is_open(menu))
                    self.assertTrue(menu.locator("a").first.is_visible())
                    self.page.keyboard.press("Escape")
                    every_delay_later()
                    self.assertFalse(is_open(menu))
                    self.assertFalse(menu.is_visible())
