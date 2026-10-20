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
from urllib.parse import urlsplit

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
