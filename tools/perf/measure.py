"""What one signed-in server costs per page: on the server, and in a browser.

The server's time is the `app` metric of its Server-Timing header, so the
client's own overhead is not in it. The browser's numbers are the platform's
own (Resource Timing, ``OBSERVE``), taken in Chromium since WebKit reports
neither layout shifts nor long tasks.
"""

import http.client
import statistics
from contextlib import ExitStack, closing
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from hq.platform.core.tests.browser.in_flight import InFlight
from tools.perf import parse

# Asked of each page before its timed requests, so a first request's caching is not its cost.
WARM = 2
# A page that sends a reader on, such as a list that opens its one record, is measured where it
# lands, within a few hops of the same server.
REDIRECTS = frozenset({301, 302, 303, 307, 308})
HOPS = 5
# Installed before any of a page's own script, in every document a visit
# loads: first contentful paint, the largest contentful paint so far (the
# latest candidate the browser reported), layout shift as Core Web Vitals
# scores it (the largest session of shifts, each under a second from the last
# and the session under five, leaving out any that follows input) and the
# main thread's long tasks.
OBSERVE = """(() => {
  const perf = { fcp: null, lcp: null, cls: 0, longTasks: 0, longTaskMs: 0 };
  new PerformanceObserver((list) => {
    for (const paint of list.getEntriesByName('first-contentful-paint')) perf.fcp = paint.startTime;
  }).observe({ type: 'paint', buffered: true });
  new PerformanceObserver((list) => {
    for (const candidate of list.getEntries()) perf.lcp = candidate.startTime;
  }).observe({ type: 'largest-contentful-paint', buffered: true });
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
# The resource types a load's wire bytes are counted by, beside its total; a
# type none of them names (a fragment's JSON, a manifest) counts in `other`.
TYPES = ("document", "stylesheet", "script", "font", "image")
# What the document and everything it fetched cost the network, an entry
# answered from the cache transferring nothing, by type, and when it painted.
# A type is the response's media type (`contentType`), or the path's extension
# where the browser reports none. `static` and `static_coded` count the
# stylesheets and scripts fetched and those that came compressed (fewer bytes
# encoded than decoded): a server that sends none compressed serves its assets
# as no production server does (`uncompressed`).
FETCHED = """() => {
  const entries = [...performance.getEntriesByType('navigation'), ...performance.getEntriesByType('resource')];
  const fetched = entries.filter((entry) => entry.transferSize > 0);
  const byType = { document: 0, stylesheet: 0, script: 0, font: 0, image: 0, other: 0 };
  const typeOf = (entry) => {
    if (entry.entryType === 'navigation') return 'document';
    const media = (entry.contentType || '').toLowerCase();
    const extension = new URL(entry.name).pathname.split('.').pop().toLowerCase();
    if (media === 'text/css' || (!media && extension === 'css')) return 'stylesheet';
    if (media.includes('javascript') || (!media && ['js', 'mjs'].includes(extension))) return 'script';
    if (media.startsWith('font/') || (!media && ['woff2', 'woff', 'ttf', 'otf'].includes(extension))) return 'font';
    if (media.startsWith('image/') || (!media && ['svg', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'ico'].includes(extension))) return 'image';
    if (media === 'text/html') return 'document';
    return 'other';
  };
  let assets = 0, coded = 0;
  for (const entry of fetched) {
    const type = typeOf(entry);
    byType[type] += entry.transferSize;
    if (type === 'stylesheet' || type === 'script') {
      assets += 1;
      if (entry.encodedBodySize < entry.decodedBodySize) coded += 1;
    }
  }
  const paint = (ms) => (ms === null ? null : Math.round(ms * 10) / 10);
  return {
    bytes: fetched.reduce((sum, entry) => sum + entry.transferSize, 0),
    requests: fetched.length,
    by_type: byType,
    static: assets,
    static_coded: coded,
    fcp_ms: paint(window.__hqPerf.fcp),
    lcp_ms: paint(window.__hqPerf.lcp),
  };
}"""


def uncompressed(seen: dict[str, Any]) -> bool:
    """Whether a load fetched stylesheets or scripts and none came compressed, as a server serving
    its assets live sends them (STATIC_LIVE), so its bytes are no production load's."""

    return seen.get("static", 0) > 0 and seen.get("static_coded", 0) == 0


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
        """The status, the time the server says it spent, and what it sent, for the page a reader
        lands on: a redirect within the server is followed, as a link to it is, and its time counted."""

        spent: list[float] = []
        for _ in range(HOPS):
            self.connection.request("GET", page, headers={"Cookie": self.cookie})
            response = self.connection.getresponse()
            body = response.read()
            took = parse.server_time(response.getheader("Server-Timing", ""))
            spent += [] if took is None else [took]
            location = urlsplit(response.getheader("Location", ""))
            if response.status not in REDIRECTS or location.netloc or not location.path.startswith("/"):
                break
            page = location._replace(scheme="", netloc="").geturl()
        return Answer(response.status, sum(spent) if spent else None, body)

    def close(self) -> None:
        self.connection.close()


def one_pass(url: str, cookie: tuple[str, str], pages: list[str]) -> dict[str, int]:
    """Each page asked once, by its status."""

    with closing(Server(url, cookie)) as server:
        return {page: server.get(page).status for page in pages}


def server_times(
    servers: dict[str, tuple[str, tuple[str, str]]], pages: list[str], rounds: int
) -> dict[str, dict[str, dict[str, Any]]]:
    """By server, then page: its status, the median and 95th percentile of its server time over
    ``rounds``, every time in the order asked, and the records its tables hold and its HTML's size,
    so a page drawing more rows on one side than the other is told apart from code that got slower.

    Each round walks every page, asking each server in turn, and which goes first alternates round
    by round, so a machine busy for a moment weighs on every side alike."""

    with ExitStack() as stack:
        opened = {name: stack.enter_context(closing(Server(url, cookie))) for name, (url, cookie) in servers.items()}
        _warm(opened, pages)
        times: dict[str, dict[str, list[float | None]]] = {name: {page: [] for page in pages} for name in opened}
        last: dict[tuple[str, str], Answer] = {}
        for turn in range(rounds):
            for page in pages:
                for name in _asking_order(list(opened), turn):
                    last[name, page] = answer = opened[name].get(page)
                    times[name][page].append(answer.took_ms)
    return {name: {page: _numbers(times[name][page], last[name, page]) for page in pages} for name in opened}


def _warm(opened: dict[str, Server], pages: list[str]) -> None:
    """Asks each server for each page ``WARM`` times, untimed, so no timed round pays a first visit."""

    for page in pages:
        for _ in range(WARM):
            for server in opened.values():
                server.get(page)


def _asking_order(names: list[str], turn: int) -> list[str]:
    """The servers in the order a round asks them: as given on an even round, reversed on an odd one."""

    return names if turn % 2 == 0 else names[::-1]


def _numbers(times: list[float | None], last: Answer) -> dict[str, Any]:
    known = [took for took in times if took is not None]
    p50, p95 = parse.percentiles(known) if known else (None, None)
    return {
        "status": last.status,
        "p50_ms": p50,
        "p95_ms": p95,
        "times_ms": times,
        "rows": parse.data_rows(last.body.decode("utf-8", errors="replace")),
        "html_bytes": len(last.body),
    }


def moved(base: list[float | None], here: list[float | None]) -> dict[str, float] | None:
    """The median of the round-by-round difference in server time, and its interquartile range: a
    difference inside its spread is noise. None where no round has both times."""

    differences = [mine - theirs for theirs, mine in zip(base, here, strict=True) if None not in (mine, theirs)]
    if not differences:
        return None
    quartiles = statistics.quantiles(differences, n=4) if len(differences) > 1 else [0.0, 0.0, 0.0]
    return {"moved_ms": round(statistics.median(differences), 2), "spread_ms": round(quartiles[2] - quartiles[0], 2)}


def past_spread(server: dict[str, Any]) -> bool:
    """Whether a page's server time moved from the base's past its spread. The whole interquartile
    range, since a run judges every page at once: past half of it, about one page in forty does by
    chance."""

    return server.get("moved_ms") is not None and abs(server["moved_ms"]) > server["spread_ms"]


# The main thread's script time, the one figure with no web API, so it is
# Chromium's own Performance.getMetrics.
def _script_seconds(session: Any) -> float:
    metrics = session.send("Performance.getMetrics")["metrics"]
    return next((metric["value"] for metric in metrics if metric["name"] == "ScriptDuration"), 0.0)


def _visit(page: Any, address: str, waiting: InFlight) -> dict[str, float | None]:
    """What loading ``address`` fetched and when it painted, read once it has loaded and nothing it
    asked for is pending."""

    page.goto(address, wait_until="load")
    waiting.settle(address)
    return page.evaluate(FETCHED)


def visit(browser: Any, url: str, cookie: tuple[str, str], page_path: str, viewport: dict[str, int]) -> dict[str, Any]:
    """A first visit to a page with an empty cache, then a repeat visit with what it cached."""

    context = browser.new_context(viewport=viewport)
    try:
        context.add_cookies([{"name": cookie[0], "value": cookie[1], "url": url}])
        context.add_init_script(OBSERVE)
        page = context.new_page()
        waiting = InFlight(page)
        session = context.new_cdp_session(page)
        session.send("Performance.enable")
        before = _script_seconds(session)
        first = _visit(page, url + page_path, waiting)
        script_ms = max(0.0, _script_seconds(session) - before) * 1000
        seen = page.evaluate("() => window.__hqPerf")
        repeat = _visit(page, url + page_path, waiting)
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
    """Each page at each viewport, by page and then by width, after a visit nothing is read from:
    a browser's first visit spends several times a later one's script time, which would be counted
    against whichever page came first, so a page's numbers would hang on what else was asked."""

    visit(browser, url, cookie, pages[0], viewports[0])
    measured: dict[str, dict[str, Any]] = {page: {} for page in pages}
    for viewport in viewports:
        for page in pages:
            measured[page][str(viewport["width"])] = visit(browser, url, cookie, page, viewport)
    return measured
