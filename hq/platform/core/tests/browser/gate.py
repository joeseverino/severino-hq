"""The browser gate every real-browser test is built on: one browser, pages served locally, a failure saved."""

import io
import json
import mimetypes
import os
import pickle
import re
import shlex
import tempfile
from collections import deque
from collections.abc import Mapping
from contextlib import contextmanager
from functools import cache, partial
from hashlib import sha256
from html import unescape
from itertools import pairwise
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast, override
from unittest import TestResult, mock
from urllib.parse import urlsplit

from django.conf import settings
from django.test import Client, RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils.csp import CSP
lazy from playwright.sync_api import Error as BrowserError

from hq.platform.core.csp_views import _blocked, _csp_violations, _directive
from hq.platform.core.run_report import Cost, tally
from hq.platform.core.speculation import rules_document
from hq.platform.core.stalls import stall_watch
from hq.platform.core.stylesheets import stylesheet
from hq.platform.core.test_runner import browser_failures
from hq.platform.core.tests.browser.probes.axe import rules_read
from hq.platform.core.tests.browser.probes.palette import (
    FILL_PAINTERS,
    FILL_STEPS,
    LINE_CONTRAST,
    PAINT_IN,
    losses,
    lost_fills,
)
from hq.platform.core.tests.browser.probes.settle import (
    DEAF_WHILE_PROBED,
    FONTS_READY,
    PAINTED,
    PUT_BACK,
    SETTLE,
    STAND_IN,
    STILL,
    hushed,
)
from hq.platform.core.tests.browser.probes.tables import PRESS, TABLE_LAYOUT, UNPRESS, tables_read
from hq.platform.core.tests.browser.reading import _in_one_trip, taken
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import NONCE, ORIGIN, static_file
from hq.platform.core.tests.browser.shards import share, sweeps
from hq.platform.core.tests.browser.shots import shoot_page
from hq.platform.core.tests.browser.survey import (
    AS_FINDINGS,
    CHECKS,
    _as_is,
    geometry_probes,
    palette_probes,
    table_probes,
)
from hq.platform.core.tests.browser.uncaught import Uncaught
from hq.platform.core.tests.browser.widths import LARGEST, NARROWEST, query_edges, viewport, viewport_queries

# Named for annotations alone: a class's annotations can be read as it is made
# (a dataclass's are), which would load a lazy import at once.
if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Playwright


def failure_name(params):
    """A failed subtest's screenshot name: its page, width and rule, then
    whatever else names it, as one file name."""

    first = [key for key in ("page", "width", "rule") if key in params]
    keys = [*first, *sorted(key for key in params if key not in first)]
    name = "-".join(str(params[key]) for key in keys)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:150] or "subtest"


# A failure's shot of a long page, the opened API reference above all, is a
# capture the browser lays the whole page out for, past an action's 5 s; it is
# given a navigation's 30 s, the failure it shows being already decided.
_FAILURE_SHOT_WITHIN = 30_000
# A browser that never comes up fails its class rather than holding the heavy
# turn for Playwright's own three minutes.
_LAUNCH_WITHIN = 30_000

# What a running test holds that holds the test itself; the parent has no use
# for either, and Django sends them as None.
_HOLDS_THE_TEST = ("_outcome", "_subtest")


class _Beside(pickle.Pickler):
    """A pickler that writes ``test`` as a reference wherever it meets it."""

    def __init__(self, file, test):
        super().__init__(file)
        self.test = test

    @override
    def persistent_id(self, obj):
        return "test" if obj is self.test else None


class _Back(pickle.Unpickler):
    def __init__(self, file, test):
        super().__init__(file)
        self.test = test

    @override
    def persistent_load(self, pid):
        return self.test


def pickles_beside(test, value):
    """Whether ``value`` survives a pickle that carries ``test`` as well.

    In that pickle a reference back to the test is the test already written,
    so here it stands as one rather than pickling the test again.
    """

    sent = io.BytesIO()
    try:
        _Beside(sent, test).dump(value)
        _Back(io.BytesIO(sent.getvalue()), test).load()
    except Exception:  # noqa: BLE001  # any failure to pickle leaves the value out
        return False
    return True


class _Watched:
    """A test's result, marking the test ``failed`` when it fails outside a
    subtest, and having a failed subtest's page shot as it fails.

    A parallel worker's result keeps no list of failures to look in: it
    sends each one to the parent as it happens. The mark is a plain flag on
    the test, which a worker pickles with a failed subtest.
    """

    def __init__(self, result, test):
        self.result = result
        self.test = test

    def __getattr__(self, name):
        return getattr(self.result, name)

    def addError(self, test, err):
        self.test.failed = True
        self.result.addError(test, err)

    def addFailure(self, test, err):
        self.test.failed = True
        self.result.addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        # Reported as the subtest ends, so the page is still the one it failed on.
        if err is not None and (err := self.test.as_reported(subtest.params, err)) is not None:
            self.test.subtest_failed(subtest.params)
        self.result.addSubTest(test, subtest, err)


@cache
def cuts_marked_after():
    """How long after a resize tables.js measures the tables' cuts again
    (`markTablesSoon`), in milliseconds, read from the script itself."""

    asset = static_file("js/tables.js")
    said = re.search(r"setTimeout\(markTables, (\d+)\)", asset[1].decode() if asset else "")
    if not said:
        raise AssertionError("tables.js no longer measures cuts on a timer after a resize the way the sweep waits for")
    return int(said[1])


def tab_key(browser):
    """The key that moves focus to every control: Option-Tab in WebKit, as
    Safari has it (plain Tab there skips buttons and links), Tab elsewhere."""

    return "Alt+Tab" if browser.browser_type.name == "webkit" else "Tab"


def unfold(page) -> None:
    """Opens the Filters control of a filter bar that folds its choices
    behind it, so they can be pressed as a reader presses them there."""

    fold = page.locator(SELECTORS["filter_fold"])
    if fold.count() and fold.is_visible():
        fold.click()


def add_css(page, css):
    """``css`` added to ``page`` after its own sheets, with script on or off.

    A constructed sheet, which the page's policy does not govern, where a
    ``<style>`` (Playwright's ``add_style_tag`` too) is one it refuses.
    """

    page.evaluate(
        """(css) => {
          const sheet = new CSSStyleSheet();
          sheet.replaceSync(css);
          document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
        }""",
        css,
    )


def with_script(test):
    """Run ``test`` in a context with script on: it checks the enhancement."""

    test.scripted = True
    return test


class Sent(str):
    """A page's body as HQ answered it, carrying the headers it was answered
    with, so the browser runs it under the policy it was sent with."""

    headers: dict[str, str]


def sent(body, headers):
    page = Sent(body)
    page.headers = headers
    return page


class SentFile(bytes):
    """A file HQ answered (a picture a page draws), carrying its headers."""

    headers: dict[str, str]


# A nonce as a policy names it, and a script tag as a browser serializes it
# once parsed: emptied, so a page cannot read its nonce back out of the markup.
_NONCE_SOURCE = re.compile(r"'nonce-([^']+)'")
_HIDDEN_NONCE = re.compile(r'(<script\b[^>]*\snonce=)""')


def answered_as(page, body):
    """``body``, the page as the browser holds it, answered with the headers
    ``page`` was and its script tags carrying the nonce those headers name."""

    if not isinstance(page, Sent):
        return _HIDDEN_NONCE.sub(rf'\1"{NONCE}"', body)
    named = _NONCE_SOURCE.search(page.headers.get(CSP.HEADER_ENFORCE, ""))
    return sent(_HIDDEN_NONCE.sub(rf'\1"{named[1]}"', body) if named else body, page.headers)


def answered(response):
    """A test client's answer as the browser is to receive it: its body and
    every header but its length, which Playwright counts from the body."""

    headers = {name: value for name, value in response.items() if name != "Content-Length"}
    if response.get("Content-Type", "").startswith("image/"):
        file = SentFile(response.getvalue())
        file.headers = headers
        return file
    return sent(response.content.decode(), headers)


# HQ's policy takes a style only from a sheet of its own origin, and a page a
# test puts together is answered under it, so the gate lifts each style
# attribute such a page writes into a sheet it serves. Each element is named
# by a data attribute and its declarations are written twice: unlayered, where
# they outrank every rule of HQ's layered stylesheet as an inline style does,
# and in a layer named before HQ's, where an !important one outranks HQ's own
# as an inline one does. A page HQ answered is never lifted: a style it writes
# is refused, which is the policy's point, and test_rendered holds HQ's markup
# to writing none.
_STYLE_ATTRIBUTE = re.compile(r'(<[a-zA-Z][^>]*?)\sstyle="([^"]*)"')
_HEAD = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_DOCTYPE = re.compile(r"<!doctype html>", re.IGNORECASE)
LIFTED = "/gate-lifted-styles/"
_LIFTED_SHEETS: dict[str, str] = {}


def served_sheet(css):
    """The address the gate serves ``css`` at, a sheet of the page's own
    origin, which its policy admits."""

    path = f"{LIFTED}{sha256(css.encode()).hexdigest()[:16]}.css"
    _LIFTED_SHEETS[path] = css
    return path


@cache
def lifted(body):
    """``body`` with its style attributes moved to a sheet ``LIFTED`` serves,
    linked ahead of every other so its layer is the first one named."""

    declared = []

    def named(match):
        declared.append(unescape(match[2]))
        return f'{match[1]} data-gate-style="{len(declared)}"'

    body = _STYLE_ATTRIBUTE.sub(named, body)
    if not declared:
        return body
    rules = "\n".join(f'[data-gate-style="{at}"] {{ {style} }}' for at, style in enumerate(declared, 1))
    path = served_sheet(f"@layer gate-lifted {{\n{rules}\n}}\n{rules}\n")
    opening = _HEAD.search(body) or _DOCTYPE.search(body)
    at = opening.end() if opening else 0
    return f'{body[:at]}<link rel="stylesheet" href="{path}">{body[at:]}'


def policy_reports(body):
    """Each violation a report to HQ's endpoint names, as the page's path, the
    directive and what it refused, read as the endpoint reads it."""

    try:
        violations = _csp_violations(json.loads(body))
    except ValueError:
        return [("", "an unreadable report", body[:80] or "nothing")]
    return [
        (
            urlsplit(str(violation.get("document-uri") or violation.get("documentURL") or "")).path,
            _directive(violation) or "a directive",
            _blocked(violation) or "inline",
        )
        for violation in violations
    ]


# What carries the policy: Django's two headers, and the endpoint core.middleware
# names for its reports.
POLICY_HEADERS = (CSP.HEADER_ENFORCE, CSP.HEADER_REPORT_ONLY, "Reporting-Endpoints")


def policy():
    """The policy headers HQ sends with a page that took a nonce, read off a
    real one whose nonce is ``NONCE``: what a page put together by a test is
    answered with, so its script tags carry ``NONCE``."""

    with mock.patch("django.utils.csp.generate_nonce", return_value=NONCE):
        response = Client().get(settings.LOGIN_URL)
    headers = {name: value for name, value in response.items() if name in POLICY_HEADERS}
    if f"'nonce-{NONCE}'" not in headers.get(CSP.HEADER_ENFORCE, ""):
        raise AssertionError(f"{settings.LOGIN_URL} took no nonce, so its policy admits no script.")
    return headers


def named_by_every_page():
    """What a signed-in page's headers send the browser to fetch, by path, as
    HQ's view answers it: the speculation rules."""

    path = reverse("speculation_rules")
    return {path: answered(rules_document(RequestFactory().get(path)))}


# What the page reports of a policy's refusal, with script on: the directive and
# what it refused. An engine also logs each refusal to the console, which names
# them too, and is all a page with script off has.
_REPORT_REFUSALS = """document.addEventListener('securitypolicyviolation', (event) => {
  window.hqRefused(event.effectiveDirective, event.blockedURI, event.sourceFile, event.lineNumber);
});"""

# The engine's line for an answer whose status says it failed: an answer the
# gate gave, which a class holds as its own (the estate's `unanswered`).
_FAILED_ANSWER = "Failed to load resource: the server responded with a status of"


# What Playwright's driver logs while the stall watch is on (its DEBUG and
# DEBUG_FILE, read once when the driver starts): the browser's launch, with its
# arguments and its own output, and every call with its steps, a navigation's
# included. It goes to a file, so a run that does not stall prints none of it.
DRIVER_DEBUG = "pw:browser,pw:api"

DRIVER_LOG_SHOWN = 80


def start_driver(owner, sync_playwright):
    """Playwright's driver for ``owner``'s class, its log shown in a stall report."""

    if not stall_watch.enabled:
        return sync_playwright().start()
    handle, log = tempfile.mkstemp(prefix="hq-playwright-", suffix=".log")
    os.close(handle)
    owner.addClassCleanup(os.unlink, log)
    with mock.patch.dict(os.environ, {"DEBUG": DRIVER_DEBUG, "DEBUG_FILE": log}):
        driver = sync_playwright().start()
    evidence = partial(driver_log, log)
    stall_watch.evidence.append(evidence)
    owner.addClassCleanup(stall_watch.evidence.remove, evidence)
    return driver


def driver_log(path, stream):
    """The end of the driver's log, as a stall report's evidence."""

    with Path(path).open(encoding="utf-8", errors="replace") as log:
        tail = deque(log, maxlen=DRIVER_LOG_SHOWN)
    stream.write(f"--- Playwright {DRIVER_DEBUG}, last {len(tail)} lines ({path}) ---\n")
    stream.writelines(tail)


# The ledgers a browser test keeps for the run's report (`report_costs`): each
# check's time in the page, a probe timed where it runs (`_in_one_trip`), and
# each rule's subtest, its probes' time in the page included (`hold`).
CHECK_COSTS = "Browser checks, time in the page"
RULE_COSTS = "Browser rules, time per subtest"


def findings_in(said, check):
    """A failure's findings by check, when it says them as findings do (a
    JSON list, or a JSON object of lists by check), or None. A list is
    ``check``'s."""

    try:
        found = json.loads(said)
    except ValueError:
        return None
    if isinstance(found, list):
        return {check: found}
    if isinstance(found, dict) and all(isinstance(lines, list) for lines in found.values()):
        return found
    return None


def signature(check, finding):
    """What a finding says, its measures aside: the same defect at another
    width or on another load differs only in its numbers."""

    said = finding if isinstance(finding, str) else json.dumps(finding, sort_keys=True)
    return check, re.sub(r"\d+", "#", said)


class BrowserGate(SimpleTestCase):
    """A browser over pages rendered once, every request answered locally.

    A subclass says what it renders (``render``); the browser then asks only
    for those paths and for static files.

    Layout must hold before progressive enhancement, so script is off unless
    the class (``scripted``) or the test (``@with_script``) checks the enhancement
    itself; ``setUp`` starts the context the test runs in.
    """

    databases = {"default"}
    scripted = False
    # A failure says its findings whole: a reader acts on all of them.
    maxDiff = None
    # Whether a test keeps Playwright's trace of what it did (``start``).
    traced = True
    # Whether `dev:look` draws the pages a subtest names by the name it gives
    # them, so a failure can say the look that finds it again (``rerun``).
    looked_at = True
    # Set once a class by setUpClass, and a test's context by start.
    policy: dict[str, str]
    pages: Mapping[Any, Any]
    served: dict[str, Sent]
    playwright: Playwright
    browser: Browser
    context: BrowserContext | None
    # Whether the context open runs the page's script, as ``start`` started it.
    runs_script = False

    @classmethod
    def render(cls):
        """Path to response body, for every page this class opens."""

        raise NotImplementedError

    @classmethod
    @override
    def setUpClass(cls):
        super().setUpClass()
        # Rendered, and rolled back, before the browser starts: Django refuses
        # database access while Playwright's event loop runs on this thread.
        # The policy first: a render may leave settings that refuse the test
        # client's host until the class is done (dev:look's estate).
        cls.policy = policy()
        cls.pages = cls.render()
        cls.served = named_by_every_page()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Run: mise run browser") from exc
        cls.playwright = start_driver(cls, sync_playwright)
        cls.addClassCleanup(cls.playwright.stop)
        engine = os.environ.get("HQ_BROWSER_ENGINE", "webkit")
        if engine not in {"chromium", "firefox", "webkit"}:
            raise ValueError("HQ_BROWSER_ENGINE must be chromium, firefox, or webkit.")
        options: dict[str, object] = {"headless": True, "timeout": _LAUNCH_WITHIN}
        if channel := os.environ.get("HQ_BROWSER_CHANNEL"):
            options["channel"] = channel
        cls.browser = getattr(cls.playwright, engine).launch(**options)
        cls.addClassCleanup(cls.browser.close)

    @override
    def setUp(self):
        self.context = None
        self.uncaught = None
        self.refusals = []
        self.swept = False
        self.addCleanup(self.read_its_share)
        self.start()
        self.addCleanup(self.close_context)
        self.addCleanup(self.capture_failure)
        # First out: a refusal no subtest took fails the test, while it can
        # still be shot.
        self.addCleanup(self.kept_the_policy)

    def mine(self, items):
        """This job's share of the pages a sweep reads (``shards.share``):
        all of them unless CI split the gate."""

        if not sweeps(self):
            raise AssertionError(f"{self.id()} reads its pages through mine: mark it @sweep, so every share runs it")
        self.swept = True
        return share(items, self.id())

    def read_its_share(self):
        """A test marked ``@sweep`` that read no pages through ``mine`` would
        run whole in every share."""

        if sweeps(self) and not self.swept and not self.failed:
            self.fail(f"{self.id()} is marked @sweep but read no pages through mine")

    def close_context(self):
        """Closes the context the test last started, if it has one, keeping
        what its page threw for the test to report."""

        if self.context is not None:
            self.refusals += self.uncaught.so_far() if self.uncaught else []
            self.context.close()

    def start(self, **options):
        """A fresh browser context, replacing any the test already has."""

        self.close_context()
        test = getattr(self, self._testMethodName, None)
        options.setdefault("java_script_enabled", getattr(test, "scripted", self.scripted))
        self.runs_script = options["java_script_enabled"]
        # Scripts asked for while this is a list wait in it, unanswered.
        self.held = None
        self.context = self.browser.new_context(**options)
        self.context.set_default_timeout(5000)
        # A page's load waits on the server, which a loaded machine slows;
        # how fast the server answers is perf's question, so a navigation
        # gets Playwright's own 30 s while an action keeps 5 s.
        self.context.set_default_navigation_timeout(30_000)
        # Playwright's record of what the test did, each step with the page as
        # it stood, kept beside a failure's screenshot (``shoot``).
        if self.traced:
            self.context.tracing.start(snapshots=True)
        if options["java_script_enabled"]:
            self.context.expose_binding("hqRefused", self.refused_by_policy)
            self.context.add_init_script(_REPORT_REFUSALS)
            self.context.add_init_script(DEAF_WHILE_PROBED)
        self.context.route("**/*", self.respond)
        self.page = self.context.new_page()
        # The sheets a test laid over the page (``lay_css``), which a load drops.
        self.laid: list[str] = []
        self.page.on("load", lambda _page: self.laid.clear())
        self.page.on("console", self.heard)
        # A report is a ping the browser sends past the routes, so it is heard
        # as a request rather than answered.
        self.page.on("request", self.reported)
        self.page.screenshot = self.shot_by(self.page.screenshot)
        self.uncaught = Uncaught(self.page, self.where_and_width)

    def shot_by(self, screenshot):
        """``screenshot``, with what the policy refuses while it is taken
        dropped: WebKit's adds a ``<style>`` of Playwright's own, which a
        page's policy refuses as it would one of the page's."""

        def shot(*args, **options):
            kept = len(self.refusals)
            try:
                return screenshot(*args, **options)
            finally:
                del self.refusals[kept:]

        return shot

    def where(self):
        parts = urlsplit(self.page.url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def refused_by_policy(self, _source, directive, blocked, source, line):
        at = f" at {source}:{line}" if source else ""
        self.refusals.append(f"{self.where()}: the policy's {directive} refused {blocked or 'inline'}{at}")

    def heard(self, message):
        if message.type == "error" and not message.text.startswith(_FAILED_ANSWER):
            self.refusals.append(f"{self.where()}: console error: {message.text}")

    def where_and_width(self):
        return f"{self.where()} at {self.page.viewport_size['width']}px"

    def refusals_so_far(self):
        """Every policy refusal, console error and uncaught script error the
        pages have had since this was last asked, each naming its page."""

        refusals, self.refusals = self.refusals, []
        return refusals + (self.uncaught.so_far() if self.uncaught else [])

    def kept_the_policy(self):
        if refusals := self.refusals_so_far():
            self.fail("\n".join(refusals))

    def fulfill(self, route, page, content_type="text/html"):
        """``page`` as the browser's answer, under the headers HQ sent it with:
        its own, for a page HQ answered, or HQ's policy, for one put together
        by a test."""

        if isinstance(page, SentFile):
            route.fulfill(headers=page.headers, body=bytes(page))
            return
        if isinstance(page, Sent):
            route.fulfill(headers=page.headers, body=str(page))
            return
        body = lifted(str(page)) if content_type == "text/html" else str(page)
        route.fulfill(headers={"Content-Type": content_type, **self.policy}, body=body)

    def settle(self):
        """Wait until the page has drawn what it just did and nothing is moving."""

        self.page.evaluate(PAINTED)
        self.page.wait_for_function(STILL)

    def holds(self, route):
        """Whether a script the page asks for is being held back, and so not
        answered yet: what its headers name is not the page's script."""

        if self.held is None or route.request.resource_type != "script":
            return False
        if urlsplit(route.request.url).path in self.served:
            return False
        self.held.append(route)
        return True

    def timed_outcomes(self, probes):
        """What each of ``probes`` returned or threw, and the seconds it took
        in the page, in one round trip the page's own scripts do not hear
        (``hushed``): ``[(True, value, s) | (False, error, s), ...]``."""

        if not probes:
            return []
        sources, args = zip(*probes, strict=True)
        return [
            (returned, value, ms / 1000)
            for returned, value, ms in self.page.evaluate(hushed(_in_one_trip(sources)), list(args))
        ]

    def outcomes(self, probes):
        """What each of ``probes`` returned or threw, in one round trip:
        ``[(True, value) | (False, error), ...]``."""

        return [outcome[:2] for outcome in self.timed_outcomes(probes)]

    def read(self, probes):
        """What each of ``probes`` returned, in one round trip."""

        return [taken(outcome) for outcome in self.outcomes(probes)]

    def respond(self, route):
        if not self.holds(route):
            self.serve(route)

    def reported(self, request):
        """A report the policy sent HQ, which is a refusal of the page's: no
        page a reader opens has one to send."""

        if urlsplit(request.url).path != settings.SEVERINO_CSP_REPORT_PATH:
            return
        body = request.post_data or ""
        for page, directive, blocked in policy_reports(body) or [
            (self.where(), "a report it reads none in", body[:300])
        ]:
            self.refusals.append(f"{page}: {settings.SEVERINO_CSP_REPORT_PATH} was sent {directive} refusing {blocked}")

    def serve(self, route):
        """What every page asks for, or what this class answers."""

        path = urlsplit(route.request.url).path
        if (served := self.served.get(path)) is not None:
            self.fulfill(route, served)
        elif (sheet := _LIFTED_SHEETS.get(path)) is not None:
            route.fulfill(content_type="text/css", body=sheet)
        else:
            self.answer(route)

    def answer(self, route):
        path = urlsplit(route.request.url).path
        name = path.strip("/")
        if name in self.pages:
            self.fulfill(route, self.pages[name], mimetypes.guess_type(name)[0] or "text/html")
            return
        if path.startswith(settings.STATIC_URL) and (asset := static_file(path.removeprefix(settings.STATIC_URL))):
            content_type, body = asset
            route.fulfill(content_type=content_type, body=body)
            return
        route.fulfill(status=404, body="Not in the synthetic fixture")

    @override
    def run(self, result=None):
        self.failed = False
        self.failures = None
        self.costs: dict[str, dict[str, Cost]] = {}
        # Each finding a failed subtest reported, by its signature, with
        # every place it was found.
        self.places: dict[tuple[str, str], tuple[str, list[str]]] = {}
        # _Watched stands in for the result, passing on what it does not keep.
        ran = super().run(result if result is None else cast(TestResult, _Watched(result, self)))
        if result is not None and self.costs and (keep := getattr(result, "addCosts", None)):
            keep(self, self.costs)
        if again := [(said, places) for said, places in self.places.values() if len(places) > 1]:
            print(f"{self.id()}: each finding reported once, at its first place, and found again:")
            for said, places in again:
                print(f"  {said}\n    at {', '.join(places)}")
        return ran

    @override
    @contextmanager
    def subTest(self, *args, **params):
        """A subtest, with a trace of its own steps: a failure keeps what the
        subtest did, never what the test did before it.

        A trace chunk holds the steps since it started (Playwright's
        ``tracing.start_chunk``); stopped with no path it is discarded
        (``_tracing.py``, ``_do_stop_chunk``). The bodies the context fetched
        (its stylesheets, its pages) stay in every chunk, so the snapshots of
        a later one still draw."""

        if self.traced and self.context is not None:
            tracing = self.context.tracing
            tracing.stop_chunk()
            tracing.start_chunk()
        with super().subTest(*args, **params):
            yield

    def spent(self, ledger, row, seconds, where=""):
        """``seconds`` more for ``row`` of ``ledger``, which the run reports."""

        tally(self.costs.setdefault(ledger, {}), row, Cost(1, seconds, seconds, where))

    @override
    def assertEqual(self, first, second, msg=None):
        """As unittest's, but what was to be found empty and was not fails
        as its findings, whole, in JSON: a rule's ``assertEqual(found, [])``
        says every finding, never unittest's shortened repr, and the subtest
        that fails reads them by check (``as_reported``)."""

        if msg is None and second in ([], {}) and first != second and isinstance(first, list | dict):
            self.fail(json.dumps(first, indent=1, default=str))
        super().assertEqual(first, second, msg)

    def lay_css(self, css):
        """``css`` laid over the page as it stands (``add_css``), kept until
        the next load so a failure's look lays it too (``rerun``)."""

        add_css(self.page, css)
        self.laid.append(css)

    def where_found(self, params):
        """Where a subtest ran, as its page and width and whatever else names it."""

        width = params.get("width") or (self.page.viewport_size or {}).get("width")
        place = " ".join(str(part) for part in (params.get("page"), f"at {width}px" if width else "") if part)
        rest = ", ".join(f"{key}={value}" for key, value in params.items() if key not in ("page", "width", "rule"))
        return ", ".join(part for part in (place, rest) if part) or self.id()

    def as_reported(self, params, err):
        """A failed subtest's error as it is reported: each finding the test
        already reported dropped, the place it was found again kept for the
        test's closing lines, and the look that finds the rest again added.
        None when nothing is left to report."""

        kind, value, trace = err
        said = str(value)
        rule = params.get("rule") or "findings"
        place = self.where_found(params)
        found = findings_in(said, rule)
        new: dict[str, list] = {}
        for check, lines in (found or {rule: [said]}).items():
            for line in lines:
                key = signature(check, line)
                if key in self.places:
                    self.places[key][1].append(place)
                else:
                    self.places[key] = (f"{check}: {line}", [place])
                    new.setdefault(check, []).append(line)
        if not new:
            return None
        if found is not None:
            said = json.dumps(new.get(rule, []) if isinstance(json.loads(said), list) else new, indent=1)
        if command := self.rerun(params):
            said += f"\n\nFound again by: {command}"
        if len(value.args) == 1 and str(value) == value.args[0]:
            value.args = (said,)
        else:
            print(said)
        return kind, value, trace

    def rerun(self, params):
        """The `dev:look` that draws a failed subtest's page as it was drawn
        and judges it by the same rule, when the subtest names a page the look
        draws: the engine, the width, script, the palette, what was opened and
        what was laid over it. A subtest that names no rule is judged by every
        check the gates hold (``survey``)."""

        if not self.looked_at or "page" not in params or self.context is None:
            return ""
        width = params.get("width") or self.page.viewport_size["width"]
        flags = [
            f"--rule {params.get('rule') or 'survey'}",
            f"--engine {self.browser.browser_type.name}",
            *(["--no-script"] if not self.runs_script else []),
            *(["--dark"] if params.get("scheme") == "dark" else []),
            *(["--open"] if params.get("disclosures") == "open" else []),
            *(["--expand"] if params.get("rows") == "open" else []),
            *([f"--css {shlex.quote(' '.join(' '.join(self.laid).split()))}"] if self.laid else []),
            f"--widths {width}",
        ]
        return " ".join(["mise run dev:look", *flags, shlex.quote(str(params["page"]))])

    @override
    def __getstate__(self):
        # A parallel worker pickles a failed subtest, and its test with it, as
        # the subtest fails and again once the test has finished; what cannot
        # be pickled (the browser it drove) stays here. Django's own filter
        # pickles each value afresh, so one that refers back to the test (a
        # bound method among its cleanups) re-enters this without end.
        kept = {key: value for key, value in vars(self).items() if key not in _HOLDS_THE_TEST}
        return {key: value for key, value in kept.items() if pickles_beside(self, value)} | dict.fromkeys(
            _HOLDS_THE_TEST
        )

    def shoot(self, name):
        """The page as it stands, saved as ``name``.png in this test's own
        folder of ``browser_failures()``, which CI keeps when a gate fails,
        and beside it, for a traced test, as ``name``.zip the trace of what
        the test did since its last failure, which `playwright show-trace` opens. The trace is
        kept first, so a shot that cannot be taken still leaves it. A page
        past what a shot holds is shot to there, and the line says so
        (``shoot_page``)."""

        if self.failures is None:
            folder = browser_failures()
            folder.mkdir(parents=True, exist_ok=True)
            self.failures = Path(tempfile.mkdtemp(prefix=f"{self._testMethodName}-", dir=folder))
        path = self.failures / f"{name}.png"
        if self.traced:
            trace = path.with_suffix(".zip")
            tracing = self.page.context.tracing
            tracing.stop_chunk(path=trace)
            tracing.start_chunk()
            print(f"Browser failure trace: {trace}")
        cut = shoot_page(self.page, path, timeout=_FAILURE_SHOT_WITHIN)
        print(f"Browser failure screenshot: {path}" + (f" ({cut})" if cut else ""))

    def subtest_failed(self, params):
        """A failed subtest's page, shot while it is still in the browser and
        named for its page, width and rule."""

        self.shoot_or_say(failure_name(params))

    def capture_failure(self):
        """A test that failed outside its subtests, shot as it ended."""

        if self.failed:
            self.shoot_or_say("page")

    def shoot_or_say(self, name):
        """``shoot(name)``, or why there is no shot when the browser cannot take
        one (it has gone with the failure, or the shot timed out): the test's
        own failure is what its report shows, never one from its cleanup."""

        try:
            self.shoot(name)
        except BrowserError as gone:
            print(f"No browser failure screenshot of {name}: {str(gone).splitlines()[0]}")

    def open(self, name, width):
        self.page.set_viewport_size(viewport(width))
        self.page.goto(f"{ORIGIN}/{name}/", wait_until="load")
        self.page.evaluate(FONTS_READY)

    def geometry(self, path, width):
        """What the layout invariants find on the page as loaded, by check:
        every one the gates hold on a page as a reader has it."""

        return self.checked(geometry_probes())

    def table_defects_at_every_width(self):
        """What the table checks (`Check(table=True)`) find on the loaded page
        at some window width from the narrowest phone up, as it was resized,
        in each state a reader can draw its tables in (rows as drawn, folds
        open, every row a reader can open opened), and whether opening rows
        moved what stayed closed, by check, each finding with the first width
        it was found at and the state it was found in; the page is left at
        the width it was loaded at, its rows closed.

        A table's room changes only where its shape changes (`TABLE_LAYOUT`):
        at an edge of the stylesheet's viewport queries, and between two edges
        wherever a box around it wraps or adds a track, or a table gives way
        or takes back what it gave, each of which moves one way as the window
        grows. Within a shape a region grows with the window and a table at
        its narrowest asks no more than it did, so a table that holds every
        check at the first width of a shape holds them at every width of it.
        So the sweep finds those widths, reading only the shape at each span's
        two ends and halving a span whose ends differ down to the first width
        of each new shape, and reads the checks once at each (`tables_read`).
        It does so with the rows the page's folds hold folded, then again with
        them open, since a table fits the rows it shows; the rows are opened
        with the folds open, so every row is read open."""

        loaded = self.page.viewport_size
        layout = [SELECTORS["table_scroll"], viewport_queries(stylesheet()), cuts_marked_after()]
        probes = table_probes()
        sources, args = zip(*probes.values(), strict=True)
        script = tables_read(hushed(_in_one_trip(sources)))
        # Each finding once, as first found: the same defect at another width
        # or in another state differs only in its measures.
        found: dict[str, dict[str, str]] = {}

        def take(check, said, where):
            kept = found.setdefault(check, {})
            for line in said:
                kept.setdefault(re.sub(r"\d+", "#", str(line)), f"{line} {where}")

        def sweep(state, rows):
            shapes: dict[int, str] = {}

            def shape(width):
                self.page.set_viewport_size(viewport(width))
                shapes[width], sizes = self.page.evaluate(TABLE_LAYOUT, layout)
                return sizes

            def between(low, high):
                if high - low > 1 and shapes[low] != shapes[high]:
                    middle = (low + high) // 2
                    shape(middle)
                    between(low, middle)
                    between(middle, high)

            # A page with no table region has nothing to sweep.
            if not shape(NARROWEST):
                return
            edges = query_edges(layout[1])
            for low, high in zip([NARROWEST, *edges], [edge - 1 for edge in edges] + [LARGEST], strict=True):
                for width in {low, high} - shapes.keys():
                    shape(width)
                between(low, high)
            widths = sorted(shapes)
            for width in [NARROWEST, *(width for before, width in pairwise(widths) if shapes[width] != shapes[before])]:
                self.page.set_viewport_size(viewport(width))
                states, moved = self.page.evaluate(
                    script, [layout[0], layout[2], state, rows, SELECTORS["own_records"], list(args)]
                )
                for said_state, outcomes in states:
                    where = f"at {width}px" + (f", {said_state}" if said_state else "")
                    for check, (returned, value, ms) in zip(probes, outcomes, strict=False):
                        self.spent(CHECK_COSTS, check, ms / 1000, f"{self.where()} {where}")
                        said = AS_FINDINGS.get(check, _as_is)(value) if returned else [f"the probe threw: {value}"]
                        take(check, said, where)
                take("opening rows moves", moved, f"at {width}px")

        folds = self.page.evaluate_handle(PRESS, SELECTORS["row_fold"])
        if self.page.evaluate("(pressed) => pressed.length", folds):
            sweep("its folds open", SELECTORS["row_opener"])
            folds.evaluate(UNPRESS)
            sweep("", None)
        else:
            sweep("", SELECTORS["row_opener"])
        folds.dispose()
        self.page.set_viewport_size(loaded)
        self.page.evaluate(SETTLE)
        return {check: list(said.values()) for check, said in found.items() if said}

    def scheme(self):
        """The palette the system asks the page for."""

        return "dark" if self.page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches") else "light"

    def lost_fills(self):
        """The fills one palette shows and the other loses, read in each on
        this load, each with what paints its fill in the palette that loses
        it; the page is left in the palette it was in."""

        reads, before = {}, []
        for palette in ("light", "dark"):
            before.append(self.in_scheme(palette))
            reads[palette] = self.page.evaluate(FILL_STEPS, [SELECTORS["contrast_scope"], SELECTORS["vendored"]])
        # A box only one reading saw came or went between them.
        both = {str(at) for at in reads["light"]["seen"]} & {str(at) for at in reads["dark"]["seen"]}
        steps = {
            palette: {at: step for at, step in read["steps"].items() if at in both} for palette, read in reads.items()
        }
        painted: dict[str, str] = {}
        for palette in ("light", "dark"):
            if places := [at for at, lost in losses(steps, LINE_CONTRAST) if lost == palette]:
                self.in_scheme(palette)
                painted |= self.read([(FILL_PAINTERS, [places, SELECTORS["vendored"]])])[0]
        self.in_scheme(before[0])
        return lost_fills(steps, LINE_CONTRAST, painted)

    def in_scheme(self, scheme):
        """The same load painted in ``scheme``'s palette (``PAINT_IN``), or as
        the system asks when ``scheme`` is none; the answer is the palette it
        was painted in before, to put back."""

        return self.page.evaluate(PAINT_IN, scheme)

    def draws_a_table(self):
        """Whether the page holds a table, shown or not at this width."""

        return self.page.locator("table").count() > 0

    def palette(self):
        """What depends on the palette: text, and the edges of fields and
        buttons, against what they sit on."""

        return self.checked(palette_probes())

    def findings(self, path, width):
        """What the layout rules find in HQ's own markup, checks that found
        nothing left out: the geometry and the palette checks in the palette
        loaded, and the fills in both; on a page with no script, only those
        that hold without it. The vendored markup is stood in for while they
        read, and put back however they end: each probe runs on its own."""

        geometry = dict(geometry_probes(self.runs_script))
        if "axe" in geometry and (left_out := self.left_out_on(path)):
            geometry["axe"] = rules_read(left_out)
        found = self.checked(
            {
                "stand in": (STAND_IN, SELECTORS["vendored"]),
                **geometry,
                **palette_probes(),
                "put back": (PUT_BACK, None),
            }
        )
        began = perf_counter()
        found["fills"] = self.lost_fills()
        # Its reads are several trips, each palette's, so it is timed here.
        self.spent(CHECK_COSTS, "fills", perf_counter() - began, self.where_and_width())
        return {check: said for check, said in found.items() if said}

    def left_out_on(self, _path) -> tuple[str, ...]:
        """The axe rules the page at ``_path`` declares it leaves out, beside
        those every page does (probes/axe.py's LEFT_OUT): none but where a
        page's class says."""

        return ()

    def checked(self, probes):
        """Each check's findings, read in one round trip, each check's time
        in the page kept for the run's ledger; a probe that threw is a finding
        of its own check, so it hides no other."""

        outcomes = self.timed_outcomes(list(probes.values()))
        where = self.where_and_width()
        found = {}
        for check, (returned, value, seconds) in zip(probes, outcomes, strict=True):
            if check in CHECKS:
                self.spent(CHECK_COSTS, check, seconds, where)
            found[check] = AS_FINDINGS.get(check, _as_is)(value) if returned else [f"the probe threw: {value}"]
        return found
