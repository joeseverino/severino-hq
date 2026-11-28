"""Waiting until a page has finished: nothing it asked for is still on its way.

Every request a page makes ends in Playwright's `requestfinished` or
`requestfailed`, so a count of those against `request` that stays at zero is a
page that has loaded; `networkidle` only guesses from 500 ms of silence. The
layout gate and perf wait the same way.
"""

from typing import Any

from hq.platform.core.tests.browser.probes.settle import SETTLE

# The wait in words, kept in perf's plan so a report that waited
# otherwise never stands in for one measured so.
SETTLED = "no request in flight two frames running after load"


class InFlight:
    """The requests a page has asked for and not had answered, counted from its own events."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.count = 0
        page.on("request", lambda request: self.moved(request, 1))
        page.on("requestfinished", lambda request: self.moved(request, -1))
        page.on("requestfailed", lambda request: self.moved(request, -1))

    def moved(self, request: Any, by: int) -> None:
        # A live page holds its event stream open for as long as it is shown,
        # so the stream is never a request still on its way.
        if request.resource_type != "eventsource":
            self.count += by

    def settle(self, what: str, frames: int = 600) -> None:
        """Wait out the page a frame at a time: each frame lets a script start what an answer asked
        for, so two in a row with nothing in flight is a page that has finished."""

        quiet = 0
        for _frame in range(frames):
            self.page.evaluate(SETTLE)
            quiet = quiet + 1 if self.count == 0 else 0
            if quiet == 2:
                return
        raise TimeoutError(f"{what} still had {self.count} requests in flight after {frames} frames")
