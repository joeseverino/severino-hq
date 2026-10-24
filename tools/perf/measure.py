"""What one signed-in server costs per page: on the server, and in a browser.

The server's time is what it says it spent, the `app` metric of the
Server-Timing header every response carries (hq/platform/core/middleware.py),
so the client's own overhead is not in it. The browser's numbers are the
platform's own: Resource Timing for bytes and requests, PerformanceObserver
for layout shift and long tasks, and Chromium's Performance.getMetrics for the
main thread's script time, the one figure with no web API. That is why the
browser is Chromium: WebKit reports neither layout shifts nor long tasks.
"""

import http.client
from contextlib import closing
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from tools.perf import parse

# Asked of each page before its timed requests, so a first request's caching is not its cost.
WARM = 2
# Installed before any of a page's own script: layout shift as Core Web Vitals
# scores it (the largest session of shifts, each under a second from the last
# and the session under five, leaving out any that follows input) and the
# main thread's long tasks.
OBSERVE = """(() => {
  const perf = { cls: 0, longTasks: 0, longTaskMs: 0 };
  let session = 0, first = 0, last = 0;
  new PerformanceObserver((list) => {
    for (const shift of list.getEntries()) {
      if (shift.hadRecentInput) continue;
      if (session && shift.startTime - last < 1000 && shift.startTime - first < 5000) {
        session += shift.value;
      } else {
        session = shift.value;
        first = shift.startTime;
      }
      last = shift.startTime;
      perf.cls = Math.max(perf.cls, session);
    }
  }).observe({ type: 'layout-shift', buffered: true });
  new PerformanceObserver((list) => {
    for (const task of list.getEntries()) {
      perf.longTasks += 1;
      perf.longTaskMs += task.duration;
    }
  }).observe({ type: 'longtask', buffered: true });
  Object.defineProperty(window, '__hqPerf', { value: perf });
})();"""
# What the document and everything it fetched cost the network: an entry
# answered from the cache transferred nothing.
FETCHED = """() => {
  const entries = [...performance.getEntriesByType('navigation'), ...performance.getEntriesByType('resource')];
  const fetched = entries.filter((entry) => entry.transferSize > 0);
  return { bytes: fetched.reduce((sum, entry) => sum + entry.transferSize, 0), requests: fetched.length };
}"""


class Answer(NamedTuple):
    status: int
    took_ms: float | None
    body: bytes


class Server:
    """One kept-alive connection to a server, signed in with a session cookie."""

    def __init__(self, url: str, cookie: tuple[str, str]) -> None:
        parts = urlsplit(url)
        self.connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=60)
        self.cookie = "=".join(cookie)

    def get(self, page: str) -> Answer:
        """The status, the time the server says it spent, and what it sent."""

        self.connection.request("GET", page, headers={"Cookie": self.cookie})
        response = self.connection.getresponse()
        body = response.read()
        return Answer(response.status, parse.server_time(response.getheader("Server-Timing", "")), body)

    def close(self) -> None:
        self.connection.close()


def one_pass(url: str, cookie: tuple[str, str], pages: list[str]) -> dict[str, int]:
    """Each page asked once, by its status."""

    with closing(Server(url, cookie)) as server:
        return {page: server.get(page).status for page in pages}


def server_times(url: str, cookie: tuple[str, str], pages: list[str], requests: int) -> dict[str, dict[str, Any]]:
    """Each page's status, the median and 95th percentile of its server time over ``requests``, and
    the records its tables hold and its HTML's size, so a page drawing more rows at one ref than at
    another is told apart from code that got slower."""

    measured = {}
    with closing(Server(url, cookie)) as server:
        for page in pages:
            for _ in range(WARM):
                server.get(page)
            answers = [server.get(page) for _ in range(requests)]
            times = [answer.took_ms for answer in answers if answer.took_ms is not None]
            p50, p95 = parse.percentiles(times) if times else (None, None)
            last = answers[-1]
            measured[page] = {
                "status": last.status,
                "p50_ms": p50,
                "p95_ms": p95,
                "rows": parse.data_rows(last.body.decode("utf-8", errors="replace")),
                "html_bytes": len(last.body),
            }
    return measured


def _script_seconds(session: Any) -> float:
    metrics = session.send("Performance.getMetrics")["metrics"]
    return next((metric["value"] for metric in metrics if metric["name"] == "ScriptDuration"), 0.0)


def _visit(page: Any, address: str) -> dict[str, int]:
    page.goto(address, wait_until="load")
    page.wait_for_load_state("networkidle")
    return page.evaluate(FETCHED)


def visit(browser: Any, url: str, cookie: tuple[str, str], page_path: str, viewport: dict[str, int]) -> dict[str, Any]:
    """A first visit to a page with an empty cache, then a repeat visit with what it cached."""

    context = browser.new_context(viewport=viewport)
    try:
        context.add_cookies([{"name": cookie[0], "value": cookie[1], "url": url}])
        context.add_init_script(OBSERVE)
        page = context.new_page()
        session = context.new_cdp_session(page)
        session.send("Performance.enable")
        before = _script_seconds(session)
        first = _visit(page, url + page_path)
        script_ms = max(0.0, _script_seconds(session) - before) * 1000
        seen = page.evaluate("() => window.__hqPerf")
        repeat = _visit(page, url + page_path)
    finally:
        context.close()
    return {
        "first": first,
        "repeat": repeat,
        "script_ms": round(script_ms, 1),
        "long_tasks": seen["longTasks"],
        "long_task_ms": round(seen["longTaskMs"], 1),
        "cls": round(seen["cls"], 4),
    }


def browser_costs(
    browser: Any, url: str, cookie: tuple[str, str], pages: list[str], viewports: list[dict[str, int]]
) -> dict[str, dict[str, Any]]:
    """Each page at each viewport, by page and then by width."""

    measured: dict[str, dict[str, Any]] = {page: {} for page in pages}
    for viewport in viewports:
        for page in pages:
            measured[page][str(viewport["width"])] = visit(browser, url, cookie, page, viewport)
    return measured
