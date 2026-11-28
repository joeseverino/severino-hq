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

import re
from typing import override
from urllib.parse import urlsplit

from hq.platform.application.markup_testing import within
from hq.platform.core.tests.browser.estate import ExampleEstateGate
from hq.platform.core.tests.browser.gate import sent
from hq.platform.core.tests.browser.probes.settle import SETTLE
from hq.platform.core.tests.browser.selectors import SELECTORS

# A phone and a laptop.
LANDS_AT = (402, 1360)

# Every box a reader sees of a page's content: each part of it under the head,
# but the ask (which draws nothing) and the dialogs aside. Each is named by its
# place, never by a state class the answer changes.
BOXES = """([content, ask]) => [...document.querySelector(content).children]
.filter((box) => !box.matches(ask) && box.tagName !== 'DIALOG' && box.getClientRects().length).map((box, place) => {
  const r = box.getBoundingClientRect();
  return `${place} ${box.tagName.toLowerCase()} at ${[r.x, r.y, r.width, r.height].map(Math.round).join(',')}`;
})"""
BOXES_ARGS = [SELECTORS["content"], SELECTORS["ask"]]

# The page laid out again from nothing, its markup and its script's state as
# they stand. An engine may keep a measure from the layout a page came by in
# steps (the next face of the stack before its own, which the gates hold back
# until asked for, `serving.in_the_face`; rows swapped into a table already
# laid out) that the same markup laid out whole does not have, a row a pixel
# off in WebKit on Linux. A landing moves nothing when what it brought lays
# out as what was drawn did, so both are measured laid out whole.
LAID_OUT_AFRESH = """() => {
  const root = document.documentElement;
  root.style.setProperty('display', 'none');
  root.getBoundingClientRect();
  root.style.removeProperty('display');
}"""

# Every element's size, unrounded, with its parent's place: what a failure
# names, since a box that grew says only that something in it did.
SIZES = """(content) => {
  const all = [...document.querySelectorAll(`${content} *`)];
  const place = new Map(all.map((element, at) => [element, at]));
  return all.map((element) => {
    const box = element.getBoundingClientRect();
    const said = element.textContent.trim().replace(/\\s+/g, ' ').slice(0, 30);
    return [`${element.tagName.toLowerCase()}.${element.className} ${box.width}x${box.height} "${said}"`,
      place.get(element.parentElement) ?? -1];
  });
}"""


def _innermost(before, after):
    """The elements whose size differs and that hold no other that does."""

    differ = {at for at, (was, now) in enumerate(zip(before, after, strict=False)) if was[0] != now[0]}
    holding = {after[at][1] for at in differ}
    return [f"{before[at][0]} -> {after[at][0]}" for at in sorted(differ - holding)][:12]


# What a press is answered with: the page it was pressed on, its ask drawn
# under way with a sentence longer than a head has room for. Its stream then
# says the estate changed, and the page asked again draws it ended.
_WAITING = "Waiting for HQ's playbook worker to take it, then reading it again."
_CHANGED = "id: 1\ndata: estate\n\n"


def _under_way(page, anchor):
    """``page`` with the ask ``anchor`` names (and its copy in the head's menu)
    drawn as asks.Ask draws work under way, followed as the estate changes."""

    def live(found):
        return (
            f'{found["open"]} data-fragment="" data-fragment-live="estate" aria-busy="true"'
            f'{found["between"]}data-ask-state="queued"{found["rest"]}'
            f'<span class="ask-word">Queued</span><span class="ask-sentence">{_WAITING}</span>'
        )

    pattern = rf'(?P<open>id="{re.escape(anchor)}(?:-menu)?")(?P<between>[^>]*?)data-ask-state="idle"(?P<rest>.*?data-ask-word>)'
    return sent(re.sub(pattern, live, str(page), flags=re.DOTALL), page.headers)


# Set before the press, so a page loaded again is told apart.
_MARK = "() => { window.probeDrawn = true; }"
_STILL_DRAWN = "() => window.probeDrawn === true"
_BUSY = "() => !!document.querySelector('[data-ask][aria-busy=\"true\"]')"
_IDLE = "() => !document.querySelector('[data-ask][aria-busy=\"true\"]')"


def _head_asks(paths, pages):
    """The estate's pages whose own head holds an ask among its actions,
    read from what the server drew. A head an example draws in a shadow root
    (the Design page's gallery) is the example's, never the page's."""

    return [
        path
        for path in paths
        if any(
            "data-ask-button" in element.attrs and not element.shadowed
            for element in within(pages[path, ""], "class", "page-actions")
        )
    ]


class PressedRefreshBrowserTests(ExampleEstateGate):
    """Every page of the example estate with an ask in its head, pressed
    where a reader finds it, beside the title or in the head's menu: while
    the work is under way and once it lands nothing on the page has moved,
    and what it read is drawn in place, never offered as a link."""

    # Where the ask pressed posts, which this answers as HQ would, and the
    # ask's id and the page it was pressed on.
    pressed = ""
    asking = ""
    on = ""

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
            self.fulfill(route, _under_way(self.pages[self.on, ""], self.asking))
            return
        if path == "/live/" and self.pressed:
            route.fulfill(headers={"Content-Type": "text/event-stream"}, body=_CHANGED)
            return
        if self.pressed and request.headers.get("x-requested-with"):
            self.redrawn.append(path)
        super().answer(route)

    def press(self):
        """The head's ask, pressed beside the title, or in the head's menu
        where the head keeps it there."""

        beside = self.page.locator(self.css(SELECTORS["head_ask_button"])).first
        if beside.count() and beside.is_visible():
            pressing = beside
        else:
            self.page.locator(self.css(SELECTORS["head_menu"] + " > button")).first.click()
            pressing = self.page.locator(self.css(SELECTORS["head_menu_panel"] + " [data-ask-button]")).first
        self.pressed = urlsplit(pressing.get_attribute("formaction") or "").path
        self.asking = pressing.evaluate("(button) => button.closest('[data-ask]').id.replace(/-menu$/, '')")
        self.on = urlsplit(self.page.url).path
        self.page.evaluate(_MARK)
        pressing.click()

    def test_pressing_a_heads_ask_moves_nothing_and_draws_the_reading_in_place(self):
        pages = _head_asks(self.navigation, self.pages)
        self.assertGreater(len(pages), 1, "the estate drew almost no head with an ask")
        for path in pages:
            for width in LANDS_AT:
                with self.subTest(page=path, width=width):
                    self.pressed, self.redrawn = "", []
                    self.visit(path, width)
                    self.page.evaluate(LAID_OUT_AFRESH)
                    self.page.evaluate(SETTLE)
                    drawn = self.page.evaluate(BOXES, self.css(BOXES_ARGS))
                    sizes = self.page.evaluate(SIZES, self.css(SELECTORS["content"]))
                    self.press()
                    self.page.wait_for_function(_BUSY)
                    self.settle()
                    self.assertEqual(self.page.evaluate(BOXES, self.css(BOXES_ARGS)), drawn, "moved while under way")

                    self.page.wait_for_function(_IDLE)
                    self.in_flight.settle(f"{path} at {width}px, landed")
                    self.settle()
                    self.assertTrue(self.page.evaluate(_STILL_DRAWN), "loaded the page again")
                    as_swapped = self.page.evaluate(BOXES, self.css(BOXES_ARGS))
                    self.page.evaluate(LAID_OUT_AFRESH)
                    self.page.evaluate(SETTLE)
                    grew = _innermost(sizes, self.page.evaluate(SIZES, self.css(SELECTORS["content"])))
                    self.assertEqual(
                        self.page.evaluate(BOXES, self.css(BOXES_ARGS)),
                        drawn,
                        f"moved once landed (as swapped: {as_swapped}; what differs: {grew})",
                    )
                    self.assertFalse(
                        self.page.locator(self.css(SELECTORS["head_offer"])).count(), "offered the reading"
                    )
                    self.assertTrue(self.redrawn, "drew nothing in place")
                    self.assertEqual(self.unanswered, [])
