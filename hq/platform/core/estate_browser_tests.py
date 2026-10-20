"""The layout gate over the example estate: every page the navigation reaches,
and a record's page of each kind it lists.

``browser_tests.LayoutBrowserTests`` holds its invariants over the stress
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
from functools import partial
from html import unescape
from typing import override
from urllib.parse import parse_qsl, urljoin, urlsplit

from django.conf import settings
from django.test import Client, override_settings
from django.urls import reverse
lazy from django.db import transaction

from hq.platform.core.browser_tests import (
    _ASIDE_BESIDE_ITS_HEADING,
    _AUDIT,
    _BROKEN_WORDS,
    _CELLS,
    _CONTRAST,
    _EMPTY_FRAMES,
    _FINISH_TRANSITIONS,
    _NESTED_DISCLOSURES,
    _NESTED_FRAMES,
    _OVERLAPS,
    _PAIRS,
    _PAST_THE_GUTTER,
    _RAISED_LINKS,
    _READINGS,
    _ROW_HEIGHTS,
    _SETTLED,
    _SIDEWAYS,
    _SPILLED_LABELS,
    _SQUEEZED_TITLES,
    _TABLES,
    _TABLES_THAT_DO_NOT_FIT,
    _TOKEN_BREAKS,
    _UNEVEN_BANDS,
    _UNPADDED_CELLS,
    _UNPADDED_TILES,
    _UNSEEN_COLUMNS,
    _WRAPPED_PHRASES,
    AUDIT_RULES,
    LARGE_TEXT_CONTRAST,
    ORIGIN,
    PHONE_SCREEN,
    ROW_LINES,
    SELECTORS,
    TEXT_CONTRAST,
    BrowserGate,
    reading_problems,
    viewport,
)
lazy from hq.platform.application.domains import domain_navigation
lazy from hq.platform.core.example_estate import REQUEST, SSO, as_production, build_showcase
lazy from hq.platform.core.example_estate.names import MACHINE, SERVICES

# A phone, a tablet held upright, a laptop and a wide desktop.
WIDTHS = (PHONE_SCREEN[0], 820, 1360, 1680)
# Where a frame across most of the page has the room to be judged empty.
WIDE = 1360
THEMES = ("light", "dark")
# The API reference mounts a vendored viewer, whose markup is not HQ's to hold
# to these rules; ReferenceBrowserTests holds it to its own.
_NOT_HQS_MARKUP = {"api_reference:reference"}
# Reached from the header on every page rather than from the navigation.
_HEADER = ("action_items",)
_REGION = re.compile(r'<[^>]*\sdata-fragment="([^"]+)"[^>]*>')
_NAME = re.compile(r'\sdata-fragment-name="([^"]*)"')


def _fragments(client, page, answers, ask):
    """Every part a page fetches into itself, and the parts inside those, by
    address and part name, as the server answers them."""

    pending = [page]
    while pending:
        for region in _REGION.finditer(pending.pop()):
            url = unescape(region.group(1))
            named = _NAME.search(region.group(0))
            name = unescape(named.group(1)) if named else ""
            if (url, name) in answers:
                continue
            headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest", **({"HTTP_X_FRAGMENT": name} if name else {})}
            response = ask(client, url, **headers)
            if response.status_code == 200:
                answers[url, name] = response.content.decode()
                pending.append(answers[url, name])


def navigation():
    """Every page the navigation declares, and the header's, by address."""

    routes = [entry.route for entry in domain_navigation() if entry.route not in _NOT_HQS_MARKUP]
    return list(dict.fromkeys(reverse(route) for route in (*routes, *_HEADER)))


def records():
    """A record's page of each kind the navigation lists: the machine, the
    service with the longest name, its container (which has an upgrade to
    make) and the zone."""

    hostname, container, *_ = max(SERVICES, key=lambda service: len(service[0]))
    return [
        reverse("control_plane:machine", kwargs={"name": MACHINE}),
        reverse("control_plane:service", kwargs={"hostname": hostname}),
        reverse("control_plane:detail", kwargs={"key": f"{container}-container"}),
        reverse("control_plane:detail", kwargs={"key": "example-zone"}),
    ]


def render_estate():
    """The navigation's pages, and every page and part the browser will ask
    for, rendered for the estate's owner the way production asks:
    ``(paths, {(address, part): body})``."""

    def ask(client, url, **headers):
        return client.get(url, secure=True, follow=True, **REQUEST, **headers)

    answers = {}
    with transaction.atomic(), as_production(), override_settings(SEVERINO_POSTURE="advanced"):
        client = Client()
        client.force_login(build_showcase(), backend=SSO)
        paths = navigation() + records()
        for path in paths:
            response = ask(client, path)
            if response.status_code != 200:
                raise AssertionError(f"{path} answered {response.status_code}")
            answers[path, ""] = response.content.decode()
            _fragments(client, answers[path, ""], answers, ask)
        transaction.set_rollback(True)
    return paths, answers


class ExampleEstateBrowserTests(BrowserGate):
    """Every navigation page of the example estate keeps the layout rules."""

    @classmethod
    def render(cls):
        cls.navigation, answers = render_estate()
        return answers

    def start(self, **options):
        options.setdefault("java_script_enabled", True)
        super().start(**options)
        self.unanswered = []
        # Requests not yet answered. `respond` answers every one, so zero after
        # the load event means loaded; `networkidle` only guesses from 500 ms of
        # silence.
        self.in_flight = 0

        def opened(_request):
            self.in_flight += 1

        def closed(_request):
            self.in_flight -= 1

        self.page.on("request", opened)
        self.page.on("requestfinished", closed)
        self.page.on("requestfailed", closed)

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
            route.fulfill(content_type="text/html", body=self.pages[address, part])
        elif parts.path.startswith(settings.STATIC_URL):
            super().answer(route)
        else:
            self.unanswered.append(f"{address} {part}".strip())
            route.fulfill(status=404, body="Not in the example estate")

    def visit(self, path, width):
        self.page.set_viewport_size(viewport(width))
        self.page.goto(f"{ORIGIN}{path}", wait_until="load")
        self.finish(path, width)

    def finish(self, path, width):
        # Each frame lets a script start what an answer asked for; two quiet
        # frames in a row with nothing in flight is a page that has finished.
        quiet = 0
        for _frame in range(600):
            self.page.evaluate(_SETTLED)
            quiet = quiet + 1 if self.in_flight == 0 else 0
            if quiet == 2:
                return
        raise AssertionError(f"{path} at {width}px still had {self.in_flight} requests in flight")

    def geometry(self, path, width):
        """What the layout invariants find on the page as loaded, by check."""

        scroll = SELECTORS["table_scroll"]
        found = {
            "sideways": self.page.evaluate(_SIDEWAYS, SELECTORS["sideways"]),
            "cells": self.page.evaluate(_CELLS),
            "broken words": self.page.evaluate(_BROKEN_WORDS),
            "wrapped phrases": self.page.evaluate(_WRAPPED_PHRASES, SELECTORS["one_line"]),
            "overlaps": self.page.evaluate(_OVERLAPS),
            "grid rows": self.page.evaluate(_PAIRS),
            "bands": self.page.evaluate(_UNEVEN_BANDS, [SELECTORS["band"], SELECTORS["band_aside"]]),
            "band padding": self.page.evaluate(_UNPADDED_CELLS, SELECTORS["band_cell"]),
            "tables": self.page.evaluate(_TABLES)["found"],
            "tables fit": self.page.evaluate(_TABLES_THAT_DO_NOT_FIT, scroll),
            "unseen columns": self.page.evaluate(_UNSEEN_COLUMNS, scroll),
            "row heights": self.page.evaluate(_ROW_HEIGHTS, ROW_LINES),
            "control labels": self.page.evaluate(_SPILLED_LABELS, SELECTORS["control"]),
            "cell labels": self.page.evaluate(_SPILLED_LABELS, SELECTORS["cell_content"]),
            "raised links": self.page.evaluate(_RAISED_LINKS),
            "row disclosures": self.page.evaluate(_NESTED_DISCLOSURES),
            "tokens": self.page.evaluate(_TOKEN_BREAKS, SELECTORS["token"]),
            "heading asides": self.page.evaluate(_ASIDE_BESIDE_ITS_HEADING, SELECTORS["heading_aside"]),
            "titles": self.page.evaluate(
                _SQUEEZED_TITLES, [SELECTORS["page_title"], SELECTORS["page_actions"], SELECTORS["page_head"]]
            ),
            "gutter": self.page.evaluate(_PAST_THE_GUTTER, [SELECTORS["gutter_bound"], False]),
            "audit": [v for v in self.page.evaluate(_AUDIT)["violations"] if v["rule"] in AUDIT_RULES],
        }
        if width >= WIDE:
            found["wide frames"] = self.page.evaluate(_EMPTY_FRAMES, [SELECTORS["frame"], SELECTORS["frame_content"]])
        if path == "/":
            found["readings"] = reading_problems(self.page.locator(SELECTORS["readings"]).evaluate_all(_READINGS))
        return found

    def palette(self):
        """What depends on the palette: frames drawn by a border, and contrast."""

        report = self.page.evaluate(_CONTRAST, [SELECTORS["contrast_scope"], TEXT_CONTRAST, LARGE_TEXT_CONTRAST])
        return {
            "frames": self.page.evaluate(_NESTED_FRAMES, [SELECTORS["frame_exempt"], SELECTORS["frame_exempt_within"]]),
            "contrast": report["found"] if report["sampled"] else ["no text sampled"],
        }

    def opened(self):
        """What holds once every disclosure on the page is open."""

        found = {
            "tile padding": self.page.evaluate(
                _UNPADDED_TILES,
                [
                    SELECTORS["tile_exempt"],
                    SELECTORS["tile_control"],
                    SELECTORS["tile_detached"],
                    SELECTORS["disclosure"],
                ],
            )
        }
        self.page.evaluate(_SETTLED)
        found["sideways, opened"] = self.page.evaluate(_SIDEWAYS, SELECTORS["sideways"])
        return found

    def test_every_navigation_page_keeps_the_layout_rules(self):
        self.assertGreater(len(self.navigation), 10, "the navigation led almost nowhere")
        for path in self.navigation:
            for width in WIDTHS:
                with self.subTest(page=path, width=width):
                    self.page.emulate_media(color_scheme="light")
                    self.visit(path, width)
                    found = self.geometry(path, width)
                    for theme in THEMES:
                        self.page.emulate_media(color_scheme=theme)
                        self.page.evaluate(_SETTLED)
                        self.page.evaluate(_FINISH_TRANSITIONS)
                        found |= {f"{check} ({theme})": said for check, said in self.palette().items()}
                    found |= self.opened()
                    found["unanswered"], self.unanswered = self.unanswered, []
                    failed = {check: said for check, said in found.items() if said}
                    if failed:
                        self.fail(json.dumps(failed, indent=1))

    def test_nothing_moves_once_a_page_is_drawn(self):
        """What the server draws is where things stay: a script and a part
        fetched after the page fill room that was already there."""

        for path in self.navigation:
            for width in WIDTHS:
                with self.subTest(page=path, width=width):
                    moved = self.moved_once_drawn(f"{ORIGIN}{path}", width, partial(self.finish, path, width))
                    if moved:
                        self.fail("\n".join(moved))
                    self.unanswered = []


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
_FIRST_COLUMN = """(rows) => [...document.querySelectorAll(rows)]
  .map((row) => row.cells[0].innerText.trim().split('\\n')[0])"""


class ExampleEstateControlTests(BrowserGate):
    """Every kind of control on a list page does what it says when pressed, on
    the example estate at a phone's width and a laptop's: a filter, a search, a
    heading that sorts, the queue's own filter and the menus that hold them.
    The answers are rendered first, every address a press can ask for, keyed by
    what it asks (a blank field and the sort a list stands at by default ask
    nothing)."""

    @classmethod
    @override
    def render(cls):
        cls.default_sort, cls.word = {}, {}
        answers = {}
        with transaction.atomic(), as_production(), override_settings(SEVERINO_POSTURE="advanced"):
            client = Client()
            client.force_login(build_showcase(), backend=SSO)

            def ask(address):
                response = client.get(address, secure=True, follow=True, **REQUEST)
                if response.status_code != 200:
                    raise AssertionError(f"{address} answered {response.status_code}")
                body = response.content.decode()
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
        row = _FIRST_ROW.search(body).group(1)
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
            route.fulfill(content_type="text/html", body=self.pages[self.key(request.url)])
        elif path.startswith(settings.STATIC_URL):
            super().answer(route)
        else:
            # A part the page asks for by itself is not what these hold.
            route.fulfill(status=404, body="Not pressed here")

    @override
    def start(self, **options):
        options.setdefault("java_script_enabled", True)
        super().start(**options)

    def visit(self, path, width):
        self.page.set_viewport_size(viewport(width))
        self.page.goto(f"{ORIGIN}{path}", wait_until="load")
        self.page.evaluate(_SETTLED)

    def rows(self):
        return self.page.locator(SELECTORS["table_rows"]).count()

    def test_a_filter_narrows_its_list_keeps_it_in_the_address_and_lets_it_go(self):
        path = reverse("expenses:list")
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(path, width)
                every = self.rows()
                menu = self.page.locator(SELECTORS["table_filter"]).first
                menu.locator("summary").click()
                box = menu.locator(SELECTORS["table_filter_box"]).first
                asked = [box.get_attribute("name"), box.get_attribute("value")]
                box.check()
                self.page.wait_for_function(_ASKED, arg=asked)
                self.assertLess(self.rows(), every)
                # The answer's menu is open where the reader left it, the box ticked.
                self.assertTrue(menu.evaluate("(menu) => menu.open"))
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

                # Back is the list as it was, the address and the box agreeing.
                self.page.go_back()
                self.page.wait_for_function(_ASKED, arg=asked)
                self.assertEqual(self.rows(), 0)
                self.assertTrue(toggle.is_checked())

    def test_a_search_narrows_its_list_and_clearing_it_brings_every_row_back(self):
        path = reverse("projects:list")
        word = self.word[path]
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(path, width)
                every = self.rows()
                search = self.page.locator(SELECTORS["table_search"])
                search.fill(word)
                self.page.wait_for_function(_ASKED, arg=["q", word])
                found = self.page.locator(SELECTORS["table_rows"]).all_inner_texts()
                self.assertTrue(0 < len(found) < every)
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

    def test_a_sort_heading_orders_its_list_and_says_which_way(self):
        path = reverse("projects:list")
        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(path, width)
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
                self.assertTrue(0 < rows.count() <= every)
                marks = rows.locator(".pill").evaluate_all("(pills) => pills.map((pill) => [...pill.classList])")
                self.assertEqual([mark for mark in marks if f"pill-{chosen}" not in mark], [])

    def test_a_menu_opens_and_closes_on_escape_and_a_press_outside(self):
        def is_open(menu):
            return menu.evaluate("(menu) => menu.open")

        for width in PRESSED_AT:
            with self.subTest(width=width):
                self.visit(reverse("expenses:list"), width)
                menus = [self.page.locator(SELECTORS["table_filter"]).first]
                if width >= WIDE:
                    menus.append(self.page.locator(SELECTORS["nav_menu"]).first)
                for menu in menus:
                    menu.locator("summary").click()
                    self.assertTrue(is_open(menu))
                    self.page.keyboard.press("Escape")
                    self.assertFalse(is_open(menu))
                    menu.locator("summary").click()
                    self.assertTrue(is_open(menu))
                    self.page.locator(SELECTORS["page_title"]).click()
                    self.assertFalse(is_open(menu))
                if width < WIDE:
                    # A phone's menus are in a drawer, which Escape closes whole.
                    self.page.locator(SELECTORS["nav_open"]).click()
                    menu = self.page.locator(SELECTORS["nav_menu"]).first
                    menu.locator("summary").click()
                    self.assertTrue(is_open(menu))
                    self.assertTrue(menu.locator("a").first.is_visible())
                    self.page.keyboard.press("Escape")
                    self.assertFalse(menu.is_visible())
