"""A refresh that lands moves nothing on the page it lands on.

A page about one thing asks for its readings once it is showing, and swaps
what it read into the page in place (partials/_visit_refresh.html): every part
of the content but the ask, through fragment.js. Nothing moves once a page is
drawn (CLAUDE.md, "Frontend quality bar"), so what the swap brings may say
more or less than what was drawn, and every box stays where it stood. WebKit
keeps no layout-shift record, so the boxes are read before and after.
"""

from typing import override
from unittest import mock
from urllib.parse import urlsplit

from django.db import transaction
from django.test import Client

from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.probes.settle import FONTS_READY, SETTLE
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.widths import viewport
lazy from hq.platform.application import glance
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
