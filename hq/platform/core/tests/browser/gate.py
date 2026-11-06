"""The browser gate every real-browser test is built on: one browser, pages served locally, a failure saved."""

import io
import mimetypes
import os
import pickle
import re
import tempfile
from collections import deque
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, override
from unittest import TestResult, mock
from urllib.parse import urlsplit

from django.conf import settings
from django.test import Client, RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils.csp import CSP
lazy from playwright.sync_api import Error as BrowserError

from hq.platform.core.speculation import rules_document
from hq.platform.core.stylesheets import stylesheet
from hq.platform.core.test_runner import browser_failures, stall_watch
from hq.platform.core.tests.browser.probes.palette import (
    FILL_PAINTERS,
    FILL_STEPS,
    LINE_CONTRAST,
    losses,
    lost_fills,
)
from hq.platform.core.tests.browser.probes.settle import (
    DEAF_WHILE_PROBED,
    FINISH_TRANSITIONS,
    FONTS_READY,
    PAINTED,
    PUT_BACK,
    SETTLE,
    STAND_IN,
    STILL,
    hushed,
)
from hq.platform.core.tests.browser.probes.tables import TABLE_LAYOUT
from hq.platform.core.tests.browser.reading import _in_one_trip, taken
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import NONCE, ORIGIN, static_file
from hq.platform.core.tests.browser.shots import shoot_page
from hq.platform.core.tests.browser.survey import AS_FINDINGS, _as_is, geometry_probes, palette_probes
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
        if err is not None:
            self.test.subtest_failed(subtest.params)
        self.result.addSubTest(test, subtest, err)


def tab_key(browser):
    """The key that moves focus to every control: Option-Tab in WebKit, as
    Safari has it (plain Tab there skips buttons and links), Tab elsewhere."""

    return "Alt+Tab" if browser.browser_type.name == "webkit" else "Tab"


def add_css(page, css):
    """``css`` added to ``page`` after its own sheets, with script on or off.

    Playwright's ``add_style_tag`` waits, with no timeout, for the style's
    load event on a handler of the page's, which never runs with script off.
    """

    page.evaluate(
        "(css) => document.head.append(Object.assign(document.createElement('style'), { textContent: css }))", css
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

    return sent(
        response.content.decode(), {name: value for name, value in response.items() if name != "Content-Length"}
    )


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
    # Set once a class by setUpClass, and a test's context by start.
    policy: dict[str, str]
    pages: Mapping[Any, Any]
    served: dict[str, Sent]
    playwright: Playwright
    browser: Browser
    context: BrowserContext | None

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
        options: dict[str, object] = {"headless": True}
        if channel := os.environ.get("HQ_BROWSER_CHANNEL"):
            options["channel"] = channel
        cls.browser = getattr(cls.playwright, engine).launch(**options)
        cls.addClassCleanup(cls.browser.close)

    @override
    def setUp(self):
        self.context = None
        self.uncaught = None
        self.refusals = []
        self.start()
        self.addCleanup(self.close_context)
        self.addCleanup(self.capture_failure)
        # First out: a refusal no subtest took fails the test, while it can
        # still be shot.
        self.addCleanup(self.kept_the_policy)

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
        self.context.tracing.start(snapshots=True)
        if options["java_script_enabled"]:
            self.context.expose_binding("hqRefused", self.refused_by_policy)
            self.context.add_init_script(_REPORT_REFUSALS)
            self.context.add_init_script(DEAF_WHILE_PROBED)
        self.context.route("**/*", self.respond)
        self.page = self.context.new_page()
        self.page.on("console", self.heard)
        self.uncaught = Uncaught(self.page, self.where_and_width)

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

        headers = page.headers if isinstance(page, Sent) else {"Content-Type": content_type, **self.policy}
        route.fulfill(headers=headers, body=str(page))

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

    def outcomes(self, probes):
        """What each of ``probes`` returned or threw, in one round trip
        the page's own scripts do not hear (``hushed``):
        ``[(True, value) | (False, error), ...]``."""

        if not probes:
            return []
        sources, args = zip(*probes, strict=True)
        return [tuple(outcome) for outcome in self.page.evaluate(hushed(_in_one_trip(sources)), list(args))]

    def read(self, probes):
        """What each of ``probes`` returned, in one round trip."""

        return [taken(outcome) for outcome in self.outcomes(probes)]

    def respond(self, route):
        if not self.holds(route):
            self.serve(route)

    def serve(self, route):
        """What every page asks for, or what this class answers."""

        if (served := self.served.get(urlsplit(route.request.url).path)) is not None:
            self.fulfill(route, served)
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
        # _Watched stands in for the result, passing on what it does not keep.
        return super().run(result if result is None else cast(TestResult, _Watched(result, self)))

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
        and beside it as ``name``.zip the trace of what the test did since
        its last failure, which `playwright show-trace` opens. The trace is
        kept first, so a shot that cannot be taken still leaves it. A page
        past what a shot holds is shot to there, and the line says so
        (``shoot_page``)."""

        if self.failures is None:
            folder = browser_failures()
            folder.mkdir(parents=True, exist_ok=True)
            self.failures = Path(tempfile.mkdtemp(prefix=f"{self._testMethodName}-", dir=folder))
        path = self.failures / f"{name}.png"
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

        return self.checked(geometry_probes(path, width))

    def tables_that_do_not_fit_at_any_width(self):
        """Each table on the loaded page wider than its region at some window
        width from the narrowest phone up, with the first width it was found
        at; the page is left at the width it was loaded at.

        A table's room changes only where the layout around it changes shape
        (`TABLE_LAYOUT`): at an edge of the stylesheet's viewport queries, and
        between two edges wherever a box around it wraps or adds a track, which
        moves one way as the window grows. So the sweep reads each span between
        edges at its two ends, and halves a span whose ends differ down to the
        first width of each new shape. Within a shape a region grows with the
        window and a table at its narrowest asks no more than it did, so a
        table that fits at the first width of a shape fits at every width of
        it."""

        loaded = self.page.viewport_size
        queries = viewport_queries(stylesheet())
        arg = [SELECTORS["table_scroll"], queries]
        found: dict[str, str] = {}
        shapes: dict[int, str] = {}

        def read(width):
            self.page.set_viewport_size(viewport(width))
            shape, sizes, misfits = self.page.evaluate(TABLE_LAYOUT, arg)
            for misfit in misfits:
                table, _, by = misfit.rpartition(" +")
                found.setdefault(table, f"+{by} at {width}px")
            shapes[width] = shape
            return sizes

        def between(low, high):
            if high - low > 1 and shapes[low] != shapes[high]:
                middle = (low + high) // 2
                read(middle)
                between(low, middle)
                between(middle, high)

        # A page with no table region has nothing to sweep.
        if read(NARROWEST):
            edges = query_edges(queries)
            for low, high in zip([NARROWEST, *edges], [edge - 1 for edge in edges] + [LARGEST], strict=True):
                for width in {low, high} - shapes.keys():
                    read(width)
                between(low, high)
        self.page.set_viewport_size(loaded)
        self.page.evaluate(SETTLE)
        return found

    def scheme(self):
        """The palette the system asks the page for."""

        return "dark" if self.page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches") else "light"

    def lost_fills(self):
        """The fills one palette shows and the other loses, read in each on
        this load, each with what paints its fill in the palette that loses
        it; the page is left in the palette it was in."""

        was = self.scheme()
        reads = {}
        for palette in ("light", "dark"):
            self.in_scheme(palette)
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
        self.in_scheme(was)
        return lost_fills(steps, LINE_CONTRAST, painted)

    def in_scheme(self, scheme):
        """The same load in the other palette. Its colors are finished rather
        than waited for: without script no animation frame runs."""

        self.page.emulate_media(color_scheme=scheme)
        self.page.evaluate(FINISH_TRANSITIONS)

    def palette(self):
        """What depends on the palette: text, and the edges of fields and
        buttons, against what they sit on."""

        return self.checked(palette_probes())

    def findings(self, path, width):
        """What the layout rules find in HQ's own markup, checks that found
        nothing left out: the geometry and the palette checks in the palette
        loaded, and the fills in both.
        The vendored markup is stood in for while they read, and put back
        however they end: each probe runs on its own."""

        found = self.checked(
            {
                "stand in": (STAND_IN, SELECTORS["vendored"]),
                **geometry_probes(path, width),
                **palette_probes(),
                "put back": (PUT_BACK, None),
            }
        )
        found["fills"] = self.lost_fills()
        return {check: said for check, said in found.items() if said}

    def checked(self, probes):
        """Each check's findings, read in one round trip; a probe that threw
        is a finding of its own check, so it hides no other."""

        outcomes = self.outcomes(list(probes.values()))
        return {
            check: AS_FINDINGS.get(check, _as_is)(value) if returned else [f"the probe threw: {value}"]
            for check, (returned, value) in zip(probes, outcomes, strict=True)
        }
