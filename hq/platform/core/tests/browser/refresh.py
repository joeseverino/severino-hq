"""A refresh that lands moves nothing on the page it lands on, nor while it is under way.

A page about one thing asks for its readings once it is showing, and swaps
what it read into the page in place (partials/_visit_refresh.html): every part
of the content but the ask, through fragment.js. Nothing moves once a page is
drawn (CLAUDE.md, "Frontend quality bar"), so what the swap brings may say
more or less than what was drawn, and every box stays where it stood. A
Refresh pressed in a page's head is the same: what it says while under way
takes no room (static/js/ask.js), and its result is drawn in place. WebKit
keeps no layout-shift record, so the boxes are read before and after.
"""

import json
from typing import override
from unittest import mock
from urllib.parse import urlsplit

from django.db import transaction
from django.test import Client

from hq.platform.core.tests.browser.estate import ExampleEstateGate
from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.probes.settle import FONTS_READY, SETTLE
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.widths import viewport
lazy from hq.platform.application import glance
lazy from hq.platform.application.asks import DONE, QUEUED, Standing
lazy from hq.platform.core import seeds
lazy from hq.platform.core.example_estate import REQUEST
lazy from hq.platform.core.example_estate.testing import as_production, sign_in

# A phone, where the readings stand two to a row, and a laptop, where they
# share the greeting's row.
LANDS_AT = (402, 1360)

# The swap ask.js makes when a page's read lands (`refresh` there).
LAND = "() => hqFragment.swap(['#content > :not(.ask-quiet)'], {renew: false})"

# Every box a reader sees on the dashboard's first screen: each reading, and
# each part of the content under the head, the ask (which draws nothing) and
# the dialogs aside. Each is named by its place, never by a state class the
# answer changes.
BOXES = """([readings, content, ask]) => [
  ...document.querySelectorAll(readings),
  ...[...document.querySelector(content).children].filter((part) => !part.matches(ask) && part.tagName !== 'DIALOG'),
].filter((box) => box.getClientRects().length).map((box, place) => {
  const r = box.getBoundingClientRect();
  return `${place} ${box.tagName.toLowerCase()} at ${[r.x, r.y, r.width, r.height].map(Math.round).join(',')}`;
})"""
BOXES_ARGS = [SELECTORS["readings"], SELECTORS["content"], SELECTORS["ask"]]

# The page laid out again from nothing, its markup and its script's state as
# they stand. The gates hold a page's face back until asked for
# (`serving.in_the_face`), so a page is first laid out in the next face of the
# stack, and an engine may keep a measure from that layout (a row a pixel
# shorter in WebKit on Linux) that the same markup swapped in afresh does not
# have. A reader's browser draws the face from the first paint or never, so a
# landing is compared with the layout that browser has.
LAID_OUT_AFRESH = """() => {
  const root = document.documentElement;
  root.style.setProperty('display', 'none');
  root.getBoundingClientRect();
  root.style.removeProperty('display');
}"""


def _current(context):
    """The strip as the refresh answers it: every reading current, and
    nothing left to ask for."""

    return glance.glance_context(
        panels=tuple({**panel, "due": False, "refreshing": False} for panel in context["dashboard_panels"])
    )


def _out_of_date(context):
    """The strip as the page first drew it: every reading out of date, which
    says its age on a line of its own, and saying less than it will once
    current, as a reading may (its last part not read yet)."""

    return glance.glance_context(
        panels=tuple(
            {**panel, "due": False, "refreshing": False, "outdated": True, "caption": panel.get("caption", [])[:-1]}
            for panel in context["dashboard_panels"]
        )
    )


class RefreshLandingBrowserTests(BrowserGate):
    """The dashboard, drawn with its readings out of date, then answered by
    the refresh with them current."""

    scripted = True
    # A box that moved is read in the whole list, where its neighbors are.
    maxDiff = None
    # Whether this test's page has been opened, so the next ask of it is the refresh.
    opened = False

    @classmethod
    @override
    def render(cls):
        pages = {}
        with transaction.atomic(), as_production(), seeds.built("showcase") as owner:
            client = Client()
            sign_in(client, owner)
            real = glance.glance_context
            for name, state in (("drawn", _out_of_date), ("landed", _current)):
                with mock.patch("hq.platform.core.dashboard_views.glance_context", lambda state=state: state(real())):
                    pages[name] = answered(client.get("/", secure=True, **REQUEST))
            transaction.set_rollback(True)
        return pages

    @override
    def answer(self, route):
        if urlsplit(route.request.url).path == "/" and route.request.method == "GET":
            # The page as drawn, then as the refresh answers it.
            self.fulfill(route, self.pages["landed" if self.opened else "drawn"])
            self.opened = True
            return
        super().answer(route)

    def test_the_refresh_landing_moves_nothing_on_the_dashboard(self):
        for width in LANDS_AT:
            with self.subTest(width=width):
                self.opened = False
                self.page.set_viewport_size(viewport(width))
                self.page.goto(f"{ORIGIN}/", wait_until="load")
                self.page.evaluate(FONTS_READY)
                self.page.evaluate(SETTLE)
                drawn = self.page.evaluate(BOXES, BOXES_ARGS)
                self.assertTrue(self.page.evaluate(LAND), "the refresh placed nothing")
                self.settle()

                self.assertEqual(self.page.evaluate(BOXES, BOXES_ARGS), drawn)


# Where a pressed ask is followed. The press is answered as asks.answer
# answers a script, under way with a sentence longer than a head has room
# for; this address then answers that it is done.
_FOLLOWED = "/probe-ask-status/"
# Set before the press, so a page loaded again is told apart.
_MARK = "() => { window.probeDrawn = true; }"
_STILL_DRAWN = "() => window.probeDrawn === true"
_BUSY = "() => !!document.querySelector('[data-ask][aria-busy=\"true\"]')"
_IDLE = "() => !document.querySelector('[data-ask][aria-busy=\"true\"]')"
# The work's end is polled for at the pace ask.js keeps, well inside this.
_LANDS_WITHIN_MS = 30_000


def _head_asks(pages):
    """The estate's pages whose head holds an ask, read from what the server drew."""

    found = []
    for (path, part), body in pages.items():
        head = str(body).partition('<header class="page-head">')[2].partition("</header>")[0]
        if not part and "data-ask-button" in head:
            found.append(path)
    return found


class PressedRefreshBrowserTests(ExampleEstateGate):
    """Every page of the example estate with an ask in its head, pressed
    where a reader finds it, beside the title or in the head's menu: while
    the work is under way and once it lands nothing on the page has moved,
    and what it read is drawn in place, never offered as a link."""

    # Where the ask pressed posts, which this answers as HQ would.
    pressed = ""

    @override
    def start(self, **options):
        super().start(**options)
        # What the page asked to have drawn again since the press.
        self.redrawn = []

    @override
    def answer(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method == "POST" and path == self.pressed:
            waiting = Standing(QUEUED, "Waiting for HQ's playbook worker to take it, then reading it again.")
            body = json.dumps({**waiting.as_json(), "status": _FOLLOWED})
            route.fulfill(status=202, content_type="application/json", body=body)
            return
        if path == _FOLLOWED:
            route.fulfill(content_type="application/json", body=json.dumps(Standing(DONE, "Read.").as_json()))
            return
        if self.pressed and request.headers.get("x-requested-with"):
            self.redrawn.append(path)
        super().answer(route)

    def press(self):
        """The head's ask, pressed beside the title, or in the head's menu
        where the head keeps it there."""

        beside = self.page.locator(SELECTORS["head_ask_button"]).first
        if beside.count() and beside.is_visible():
            pressing = beside
        else:
            self.page.locator(f"{SELECTORS['head_menu']} > button").first.click()
            pressing = self.page.locator(f"{SELECTORS['head_menu_panel']} [data-ask-button]").first
        self.pressed = urlsplit(pressing.get_attribute("formaction") or "").path
        self.page.evaluate(_MARK)
        pressing.click()

    def test_pressing_a_heads_ask_moves_nothing_and_draws_the_reading_in_place(self):
        pages = _head_asks(self.pages)
        self.assertGreater(len(pages), 1, "the estate drew almost no head with an ask")
        for path in pages:
            for width in LANDS_AT:
                with self.subTest(page=path, width=width):
                    self.pressed, self.redrawn = "", []
                    self.visit(path, width)
                    self.page.evaluate(LAID_OUT_AFRESH)
                    self.page.evaluate(SETTLE)
                    drawn = self.page.evaluate(BOXES, BOXES_ARGS)
                    self.press()
                    self.page.wait_for_function(_BUSY)
                    self.settle()
                    self.assertEqual(self.page.evaluate(BOXES, BOXES_ARGS), drawn, "moved while under way")

                    self.page.wait_for_function(_IDLE, timeout=_LANDS_WITHIN_MS)
                    self.in_flight.settle(f"{path} at {width}px, landed")
                    self.settle()
                    self.assertTrue(self.page.evaluate(_STILL_DRAWN), "loaded the page again")
                    self.assertEqual(self.page.evaluate(BOXES, BOXES_ARGS), drawn, "moved once landed")
                    self.assertFalse(self.page.locator(SELECTORS["head_offer"]).count(), "offered the reading")
                    self.assertTrue(self.redrawn, "drew nothing in place")
                    self.assertEqual(self.unanswered, [])
