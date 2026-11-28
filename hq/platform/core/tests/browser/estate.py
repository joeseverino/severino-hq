"""The layout gate over the example estate: every page the navigation reaches,
and a record's page of each kind it lists.

``layout.LayoutBrowserTests`` holds its invariants over the stress
estate, which is built to break layouts and covers the pages it was built for.
This holds the same invariants over the homelab the demo shows
(``hq/platform/core/example_estate``), on every page a person reaches from the
navigation, as a reader sees it: script on, deferred parts loaded, at a phone,
a tablet and a laptop, in the light palette and the dark.

Each page is rendered once, opened once per width, and every check runs on
that one load; a finding names its check, page, width and theme.
"""

import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import cache
from html import unescape
from types import MappingProxyType
from typing import Any, override
from urllib.parse import parse_qsl, urljoin, urlsplit

from django.conf import settings
from django.test import Client
from django.urls import reverse
lazy from django.db import transaction

from hq.platform.core.tests.browser.gate import BrowserGate, answered, sent, unfold
from hq.platform.core.tests.browser.in_flight import InFlight
from hq.platform.core.tests.browser.probes.boxes import SIDEWAYS
from hq.platform.core.tests.browser.probes.settle import FONTS_READY, SETTLE
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.shards import sweep
from hq.platform.core.tests.browser.survey import geometry_probes
from hq.platform.core.tests.browser.widths import PALETTE_AT, PHONE_SCREEN, SCRIPTED, WIDE, viewport
lazy from hq.platform.application import fragments
lazy from hq.platform.application.domains import domain_navigation
lazy from hq.platform.core import seeds
lazy from hq.platform.core.example_estate import REQUEST
lazy from hq.platform.core.example_estate.names import MACHINE, SERVICES
lazy from hq.platform.core.example_estate.network import ZONE_KEY
lazy from hq.platform.core.example_estate.records import RUNBOOKS, SERVER_ASSET, SWITCH_ASSET, UNFILED_RECEIPT, WRITEUPS
lazy from hq.platform.core.example_estate.testing import as_production, sign_in

# WebKit reads a page as the sweeps read one once its script has run
# (`SCRIPTED`), as HQ is read in Safari; another engine is a second opinion
# at a phone and a laptop.
SECOND_OPINION_AT = (PHONE_SCREEN[0], WIDE)
THEMES = ("light", "dark")
# A page the policy refuses a style element on, answered as HQ answers one,
# opened last: a browser sends a report once it gets to it, so the report this
# page draws is what the sweep waits for, and it comes after any the pages
# before it drew.
_REFUSED = "/example-refused/"
_STYLED = '<!doctype html><html><head><meta charset="utf-8"><style>p { color: red }</style></head><body><p>Example</p></body></html>'
# The API reference mounts a vendored viewer, whose markup is not HQ's to hold
# to these rules.
_NOT_HQS_MARKUP = {"api_reference:reference"}
# Reached from the header on every page rather than from the navigation.
_HEADER = ("action_items",)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# The checks a shown popover's own boxes and lines are held to: its rows are
# laid out by the same primitives as the page's, over whatever lies beneath.
_IN_A_POPOVER = ("overlaps", "overlapping text", "touching text")


_EMBEDDED = re.compile(r'<img\b[^>]*\ssrc="(/[^"/][^"]*)"')


def embedded_of(pages, ask):
    """Each picture ``pages`` draw from HQ's own records (a receipt's file),
    by its address, as the server answers it: a browser asks for it as the
    page loads."""

    addresses = {
        unescape(url) for page in pages for url in _EMBEDDED.findall(page) if not url.startswith(settings.STATIC_URL)
    }
    asked = {url: ask(url) for url in sorted(addresses)}
    return {(url, ""): answered(response) for url, response in asked.items() if response.status_code == 200}


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
    make), the zone, a message with the page that deletes it, HQ's project
    (the one every other kind is about), the server as an asset and its
    expense and receipt, the switch nothing else is about, the receipt not
    yet filed (a picture) and the expense its match page offers, a runbook
    and a writeup; then each one an installed extension's demo names
    (``seeds.demo_pages``)."""

    hostname, container, *_ = max(SERVICES, key=lambda service: len(service[0]))
    return [
        reverse("control_plane:machine", kwargs={"name": MACHINE}),
        reverse("control_plane:service", kwargs={"hostname": hostname}),
        reverse("control_plane:detail", kwargs={"key": f"{container}-container"}),
        reverse("control_plane:detail", kwargs={"key": ZONE_KEY}),
        reverse("contacts:detail", args=[1]),
        reverse("contacts:delete", args=[1]),
        reverse("projects:detail", kwargs={"slug": "hq"}),
        reverse("assets:detail", kwargs={"slug": SERVER_ASSET}),
        reverse("expenses:detail", args=[1]),
        reverse("receipts:detail", args=[1]),
        reverse("assets:detail", kwargs={"slug": SWITCH_ASSET}),
        reverse("receipts:detail", args=[UNFILED_RECEIPT]),
        reverse("receipts:match", args=[UNFILED_RECEIPT]),
        reverse("docs_index:detail", kwargs={"doc_id": RUNBOOKS[0][0]}),
        reverse("content:detail", kwargs={"slug": WRITEUPS[0][0]}),
        *seeds.demo_pages(),
    ]


def opened():
    """Each page drawn with a folded part open, at the address its placeholder
    falls back to (``data-fragment-fallback``): loaded as a reader first meets
    it, the fold stays closed and its part is never drawn."""

    agents = reverse("agent_policy")
    return [f"{agents}?endpoint=1", f"{agents}?all=1"]


def estate_pages():
    """Every page of the example estate the gate reads and dev:look's ``all``
    stands for, by address: the navigation's, each with its folded parts open,
    then a record's page of each kind."""

    return [*navigation(), *opened(), *records()]


def render_estate():
    """The navigation's pages, and every page and part the browser will ask
    for, rendered for the estate's owner the way production asks:
    ``(paths, {(address, part): body})``."""

    def ask(client, url, **headers):
        return client.get(url, secure=True, follow=True, **REQUEST, **headers)

    answers = {}
    with owner_client() as client:
        paths = landings(lambda address: ask_as_production(client, address), estate_pages())
        for path in paths:
            # Asked as listed, unfollowed: a page in the list is the page itself, never a redirect to one.
            response = ask_as_production(client, path)
            if response.status_code != 200:
                raise AssertionError(f"{path} answered {response.status_code}")
            answers[path, ""] = answered(response)
            fragments_of(client, path, answers[path, ""], answers, ask)
        answers |= embedded_of(list(answers.values()), lambda url: ask(client, url))
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

    def visit(self, path, width):
        self.page.set_viewport_size(viewport(width))
        self.page.goto(f"{ORIGIN}{path}", wait_until="load")
        # A page without script runs no frame callback, so a wait on one never
        # ends, and starts no request of its own once loaded.
        if self.runs_script:
            self.in_flight.settle(f"{path} at {width}px")
        self.page.evaluate(FONTS_READY)


class ExampleEstateBrowserTests(ExampleEstateGate):
    """Every navigation page of the example estate keeps the layout rules."""

    def opened(self, path, width):
        """What holds once every disclosure on the page is open, and inside
        each kind of popover shown."""

        self.page.evaluate(
            "(disclosure) => document.querySelectorAll(disclosure).forEach((d) => { d.open = true; })",
            self.css(SELECTORS["disclosure"]),
        )
        self.page.evaluate(SETTLE)
        found = {"sideways, opened": self.page.evaluate(SIDEWAYS, self.css(SELECTORS["sideways"]))}
        probes = {check: probe for check, probe in geometry_probes().items() if check in _IN_A_POPOVER}
        for (label, _slug), _alike in self.each_popover_kind():
            self.page.evaluate(SETTLE)
            found |= {f"{check}, with {label} shown": said for check, said in self.checked(probes).items()}
        return found

    @sweep
    def test_every_navigation_page_keeps_the_layout_rules(self):
        """Each page keeps the layout rules and sends ``/csp-report/`` nothing."""

        self.assertGreater(len(self.navigation), 10, "the navigation led almost nowhere")
        webkit = self.browser.browser_type.name == "webkit"
        for path in self.mine(self.navigation):
            for width in SCRIPTED.at(self.draws_a_table) if webkit else SECOND_OPINION_AT:
                with self.subTest(page=path, width=width):
                    self.visit(path, width)
                    found = self.geometry(path, width)
                    for theme in THEMES if width in PALETTE_AT else ():
                        was = self.in_scheme(theme)
                        self.page.evaluate(SETTLE)
                        found |= {f"{check} ({theme})": said for check, said in self.palette().items()}
                        self.in_scheme(was)
                    found |= self.opened(path, width)
                    found["unanswered"], self.unanswered = self.unanswered, []
                    found["refused"] = self.refusals_so_far()
                    self.assertEqual({check: said for check, said in found.items() if said}, {})
        if webkit:
            self.a_refused_page_is_heard_after_every_other()

    def a_refused_page_is_heard_after_every_other(self):
        """The refusals each page was held to are heard: a page the policy
        refuses sends its report, and none came late from the pages before it."""

        self.served = {**self.served, _REFUSED: sent(_STYLED, {"Content-Type": "text/html", **self.policy})}
        with self.page.expect_request_finished(is_report(settings.SEVERINO_CSP_REPORT_PATH)):
            self.page.goto(f"{ORIGIN}{_REFUSED}", wait_until="load")
        # A round trip, so the page's word of the refusal has reached the gate too.
        self.settle()
        heard = self.refusals_so_far()
        report = f"{_REFUSED}: {settings.SEVERINO_CSP_REPORT_PATH} was sent"
        self.assertTrue(
            [line for line in heard if line.startswith(report)],
            f"the refused page's report never reached {settings.SEVERINO_CSP_REPORT_PATH}: {heard}",
        )
        # A report a page before it drew late arrives before this one.
        self.assertEqual([line for line in heard if not line.startswith(_REFUSED)], [])


def is_report(path):
    return lambda request: urlsplit(request.url).path == path


# A list's toolbar boxes (a filter's options and its toggles), the sort its
# server stands at, the links its headings sort by, and the queue's choices.
_TOOLBAR_BOX = re.compile(r'<input type="checkbox" name="([^"]+)" value="([^"]*)"')
_DEFAULT_SORT = re.compile(r'<select name="sort">.*?<option value="([^"]*)" selected>', re.DOTALL)
_SORT_LINK = re.compile(r'<a class="table-sort-link"[^>]* href="([^"]+)"')
_QUEUE_CHOICES = re.compile(r'<select name="(status|source)"[^>]*>(.*?)</select>', re.DOTALL)
_OPTION = re.compile(r'<option value="([^"]+)"')
_FIRST_ROW = re.compile(r"<tbody\b[^>]*>\s*<tr[^>]*>(.*?)</tr>", re.DOTALL)
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
  .flatMap((heading, place) => !heading.checkVisibility() ? [place] : [])"""
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
        sorts = _SORT_LINK.findall(body)
        if not sorts:
            raise AssertionError(f"{path} draws no heading that sorts")
        for href in sorts:
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
        unnamed = {cls.default_sort.get(parts.path)}
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
        return self.page.locator(self.css(SELECTORS["table_rows"])).count()

    def test_a_filter_narrows_its_list_keeps_it_in_the_address_and_lets_it_go(self):
        path = reverse("expenses:list")
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(path, width)
                # Every answer below is drawn on this page, never by loading another.
                self.page.evaluate("() => { window.kept = true; }")
                every = self.rows()
                unfold(self.page)
                self.page.locator(self.css(SELECTORS["table_filter"])).first.click()
                menu = self.page.locator(self.css(SELECTORS["table_filter_panel"])).first
                box = menu.locator(self.css(SELECTORS["table_filter_box"])).first
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

                # A toggle beside the menus narrows the list too, and gives every row back.
                toggle = self.page.locator(self.css(SELECTORS["table_toggle"])).first
                asked = [toggle.get_attribute("name"), toggle.get_attribute("value")]
                toggle.check()
                self.page.wait_for_function(_ASKED, arg=asked)
                kept = self.rows()
                self.assertLess(kept, every)
                toggle.uncheck()
                self.page.wait_for_function(_LET_GO, arg=asked)
                self.assertEqual(self.rows(), every)

                # Back is the list as it was, the address and the box agreeing,
                # and Forward the list after it.
                self.page.go_back()
                self.page.wait_for_function(_ASKED, arg=asked)
                self.assertEqual(self.rows(), kept)
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
                search = self.page.locator(self.css(SELECTORS["table_search"]))
                search.fill(word)
                self.page.wait_for_function(_ASKED, arg=["q", word])
                found = self.page.locator(self.css(SELECTORS["table_rows"])).all_inner_texts()
                self.assertGreater(len(found), 0)
                self.assertLess(len(found), every)
                self.assertEqual([row for row in found if word.lower() not in row.lower()], [])
                # Typing carries on in the box: the answer did not take it away.
                self.assertTrue(search.evaluate("(box) => box === document.activeElement"))
                search.fill(_NOTHING)
                self.page.wait_for_function(_ASKED, arg=["q", _NOTHING])
                self.assertEqual(self.rows(), 0)
                self.assertTrue(self.page.locator(self.css(SELECTORS["table_empty"])).is_visible())
                search.fill("")
                self.page.wait_for_function(_LET_GO, arg=["q", _NOTHING])
                self.assertEqual(self.rows(), every)
                self.assertTrue(self.page.evaluate("() => window.kept === true"), "a search loaded another page")

    def test_a_list_sorted_in_place_still_fits_a_window_that_narrows(self):
        """A sort swaps the table in to be laid out around its own rows, so a
        narrower window drops a column rather than holding the table at its
        old width past its region."""

        self.visit(reverse("projects:list"), 1360)
        link = self.page.locator(self.css(SELECTORS["sort_link"])).first
        asked = dict(parse_qsl(urlsplit(link.get_attribute("href")).query))["sort"]
        link.click()
        self.page.wait_for_function(_ASKED, arg=["sort", asked])
        self.page.set_viewport_size({"width": 400, "height": PHONE_SCREEN[1]})
        fits = """(region) => {
          const table = document.querySelector(region + ' > table');
          return table.offsetWidth <= table.parentElement.clientWidth
            && [...table.tHead.rows[0].cells].some((heading) => !heading.checkVisibility());
        }"""
        self.page.wait_for_function(fits, arg=self.css(SELECTORS["table_scroll"]))

    def test_a_list_sorted_on_a_phone_draws_no_column_it_had_dropped(self):
        """The table a sort swaps in is laid out by the stylesheet in its first
        frame, so no column the list had dropped is drawn and then dropped again."""

        self.visit(reverse("projects:list"), PHONE_SCREEN[0])
        before = self.page.evaluate(_DROPPED, self.css(SELECTORS["table_scroll"]))
        self.assertTrue(before, "a phone's list dropped no column, so the sort has nothing to keep")
        self.page.evaluate(_WATCH_DROPPED, self.css(SELECTORS["table_scroll"]))
        link = self.page.locator(self.css(SELECTORS["sort_link"])).first
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
                link = self.page.locator(self.css(SELECTORS["sort_link"])).first
                sorted_by = "(link) => link.closest('th').getAttribute('aria-sort')"
                # Unasked, the list is read in its first column's order, and that heading says so.
                for pressed, direction in enumerate(("ascending", "descending", "ascending")):
                    if pressed:
                        asked = dict(parse_qsl(urlsplit(link.get_attribute("href")).query))["sort"]
                        link.click()
                        self.page.wait_for_function(_ASKED, arg=["sort", asked])
                    self.assertEqual(link.evaluate(sorted_by), direction)
                    names = self.page.evaluate(_FIRST_COLUMN, self.css(SELECTORS["table_rows"]))
                    self.assertEqual(names, sorted(names, key=str.lower, reverse=direction == "descending"))
                self.assertTrue(self.page.evaluate("() => window.kept === true"), "a sort loaded another page")

    def test_every_queue_row_opens_what_raised_it_on_a_modifier_press_a_person_can_reach(self):
        """A row that keeps its urgency for a screen reader still opens its
        panel: a Cmd- or Ctrl-press on its row lands on the row's own cell, the
        one place a person can press, and opens that row's panel, not its fold."""

        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(reverse("action_items"), width)
                rows = self.page.locator(self.css(SELECTORS["decisions"] + ":has([data-origin])"))
                self.assertGreater(rows.count(), 0)
                for place in range(rows.count()):
                    row = rows.nth(place)
                    if not row.is_visible():
                        continue
                    gutter = row.locator(".row-gutter")
                    box = gutter.bounding_box()
                    # The cell's own far corner, clear of its title link.
                    gutter.click(modifiers=["ControlOrMeta"], position={"x": box["width"] - 4, "y": box["height"] - 4})
                    panel = row.locator(".origin-panel")
                    self.assertTrue(panel.evaluate("(panel) => panel.matches(':popover-open')"), place)
                    panel.evaluate("(panel) => panel.hidePopover()")

    def test_the_queue_keeps_only_the_urgency_it_is_asked_for(self):
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(reverse("action_items"), width)
                every = self.page.locator(self.css(SELECTORS["decisions"])).count()
                unfold(self.page)
                urgency = self.page.locator(self.css(SELECTORS["queue_urgency"]))
                # The urgency the first row is at, so the answer has rows to hold.
                offered = urgency.locator("option").evaluate_all("(options) => options.map((option) => option.value)")
                first = (
                    self.page.locator(self.css(SELECTORS["decisions"] + " .pill")).first.get_attribute("class").split()
                )
                chosen = next(value for value in offered if value and f"tone-{value}" in first)
                urgency.select_option(chosen)
                with self.page.expect_navigation():
                    self.page.locator(self.css(SELECTORS["queue_apply"])).click()
                self.assertIn(("status", chosen), parse_qsl(urlsplit(self.page.url).query))
                self.assertEqual(urgency.input_value(), chosen)
                rows = self.page.locator(self.css(SELECTORS["decisions"]))
                shown = rows.count()
                self.assertGreater(shown, 0)
                self.assertLessEqual(shown, every)
                marks = rows.locator(".pill").evaluate_all("(pills) => pills.map((pill) => [...pill.classList])")
                self.assertEqual([mark for mark in marks if f"tone-{chosen}" not in mark], [])

    def test_a_category_opened_in_the_phone_drawer_opens_in_place(self):
        """A category's menu lives inside the drawer that holds it, so pressing
        it opens the category in place, at the drawer's width under its own
        row, and leaves the drawer open around it; Escape closes the category
        alone, then the drawer."""

        self.visit(reverse("expenses:list"), PHONE_SCREEN[0])
        toggle = self.page.locator(self.css(SELECTORS["nav_toggle"]))
        toggle.click()
        drawer = self.page.locator(self.css(SELECTORS["nav_drawer"]))
        self.assertTrue(drawer.is_visible())
        handle = self.page.locator(self.css(SELECTORS["nav_menu"])).first
        links = self.page.locator(self.css(SELECTORS["nav_menu_panel"])).first.get_by_role("link")
        handle.click()
        self.assertTrue(links.first.is_visible())
        self.assertTrue(drawer.is_visible())
        # Under the drawer's last row shown, its own, and as wide as the drawer.
        drawer_box, panel_box, row_box = self.page.evaluate(
            "([drawer, panel]) => [document.querySelector(drawer), document.querySelector(panel),"
            " document.querySelector(panel).previousElementSibling].map((box) => box.getBoundingClientRect().toJSON())",
            self.css([SELECTORS["nav_drawer"], SELECTORS["nav_menu_panel"] + ":popover-open"]),
        )
        self.assertEqual((panel_box["left"], panel_box["right"]), (drawer_box["left"], drawer_box["right"]))
        self.assertGreaterEqual(panel_box["top"], row_box["bottom"])
        self.assertLessEqual(panel_box["top"], drawer_box["bottom"])
        self.page.keyboard.press("Escape")
        self.assertFalse(links.first.is_visible())
        self.assertTrue(drawer.is_visible())
        self.page.keyboard.press("Escape")
        self.assertFalse(drawer.is_visible())

    def test_each_category_in_the_phone_drawer_starts_where_a_plain_section_does(self):
        """A category's button is a row of the list, so its name starts at
        the list's edge as a plain section's link does, never centered."""

        self.visit(reverse("expenses:list"), PHONE_SCREEN[0])
        self.page.locator(self.css(SELECTORS["nav_toggle"])).click()
        starts = self.page.evaluate(
            "(drawer) => [...document.querySelector(drawer).querySelectorAll(':scope > a, .nav-group-label')]"
            ".map((entry) => { const range = document.createRange(); range.selectNodeContents(entry);"
            " return Math.round(range.getClientRects()[0].left); })",
            self.css(SELECTORS["nav_drawer"]),
        )
        self.assertEqual(len(set(starts)), 1, starts)

    def test_every_link_of_every_category_in_the_phone_drawer_can_be_reached(self):
        """However low a category sits in the drawer and however short the
        screen, each of its links can be scrolled to and pressed once it is
        open."""

        for height in (PHONE_SCREEN[1], 500):
            with self.subTest(height=height):
                self.visit(reverse("expenses:list"), PHONE_SCREEN[0])
                self.page.set_viewport_size({"width": PHONE_SCREEN[0], "height": height})
                self.page.locator(self.css(SELECTORS["nav_toggle"])).click()
                handles = self.page.locator(self.css(SELECTORS["nav_menu"]))
                for place in range(handles.count()):
                    handle = handles.nth(place)
                    handle.click()
                    # Each link scrolled to as a reader scrolls, then whole on
                    # the screen and the one thing a press at its center hits.
                    unreached = handle.evaluate(
                        "(handle) => [...handle.popoverTargetElement.querySelectorAll('a')].filter((link) => {"
                        " link.scrollIntoView({ block: 'nearest' }); const box = link.getBoundingClientRect();"
                        " const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);"
                        " return box.top < 0 || box.bottom > innerHeight || !link.contains(hit);"
                        " }).map((link) => link.textContent.trim())"
                    )
                    self.assertEqual(unreached, [], handle.text_content().strip())
                    self.page.keyboard.press("Escape")

    def test_a_hovered_section_opens_a_press_keeps_it_and_leaving_closes_it(self):
        """Where the pointer hovers, a section group opens under it; a press
        then keeps it open after the pointer leaves, and one only hovered
        closes once the pointer has gone."""

        def is_open(handle):
            return handle.evaluate("(handle) => handle.popoverTargetElement.matches(':popover-open')")

        def away():
            self.page.mouse.move(viewport(WIDE)["width"] // 2, viewport(WIDE)["height"] - 10)

        self.page.clock.install()
        self.visit(reverse("expenses:list"), WIDE)
        handles = self.page.locator(self.css(SELECTORS["nav_menu"]))
        pressed, hovered = handles.nth(0), handles.nth(1)
        pressed.hover()
        self.page.clock.run_for(1000)
        self.assertTrue(is_open(pressed))
        pressed.click()
        away()
        self.page.clock.run_for(1000)
        self.assertTrue(is_open(pressed))
        self.page.keyboard.press("Escape")
        self.assertFalse(is_open(pressed))
        hovered.hover()
        self.page.clock.run_for(1000)
        self.assertTrue(is_open(hovered))
        away()
        self.page.clock.run_for(1000)
        self.assertFalse(is_open(hovered))
