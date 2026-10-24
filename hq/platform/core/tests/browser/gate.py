"""The browser gate every real-browser test is built on: one browser, pages served locally, a failure saved."""

import mimetypes
import os
import re
import tempfile
from collections import deque
from functools import partial
from pathlib import Path
from typing import override
from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.test import SimpleTestCase
from django.test.testcases import is_pickable
lazy from playwright.sync_api import Error as BrowserError

from hq.platform.core.test_runner import stall_watch
from hq.platform.core.tests.browser.probes.palette import _FILL_STEPS, LINE_CONTRAST, lost_fills
from hq.platform.core.tests.browser.probes.settle import (
    _FINISH_TRANSITIONS,
    _FONTS_READY,
    _IN_THE_HEAD,
    _MOVED_SINCE_DRAWN,
    _PAINTED,
    _PUT_BACK,
    _SETTLED,
    _STAND_IN,
    _STILL,
    _UNTIL_DRAWN,
)
from hq.platform.core.tests.browser.reading import _in_one_trip, taken
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN, slow_frames, static_file
from hq.platform.core.tests.browser.survey import _AS_FINDINGS, _PALETTE, _as_is, geometry_probes
from hq.platform.core.tests.browser.widths import viewport


def failure_name(params):
    """A failed subtest's screenshot name: its page, width and rule, then
    whatever else names it, as one file name."""

    first = [key for key in ("page", "width", "rule") if key in params]
    keys = [*first, *sorted(key for key in params if key not in first)]
    name = "-".join(str(params[key]) for key in keys)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:150] or "subtest"


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

    @classmethod
    def render(cls):
        """Path to response body, for every page this class opens."""

        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Rendered, and rolled back, before the browser starts: Django refuses
        # database access while Playwright's event loop runs on this thread.
        cls.pages = cls.render()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Run: mise run browser") from exc
        cls.playwright = start_driver(cls, sync_playwright)
        cls.addClassCleanup(cls.playwright.stop)
        engine = os.environ.get("HQ_BROWSER_ENGINE", "webkit")
        if engine not in {"chromium", "firefox", "webkit"}:
            raise ValueError("HQ_BROWSER_ENGINE must be chromium, firefox, or webkit.")
        options = {"headless": True}
        if channel := os.environ.get("HQ_BROWSER_CHANNEL"):
            options["channel"] = channel
        cls.browser = getattr(cls.playwright, engine).launch(**options)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = None
        self.start()
        self.addCleanup(lambda: self.context.close())
        self.addCleanup(self.capture_failure)

    def start(self, **options):
        """A fresh browser context, replacing any the test already has."""

        if self.context is not None:
            self.context.close()
        test = getattr(self, self._testMethodName, None)
        options.setdefault("java_script_enabled", getattr(test, "scripted", self.scripted))
        # Scripts asked for while this is a list wait in it (moved_once_drawn).
        self.held = None
        self.context = self.browser.new_context(**options)
        self.context.set_default_timeout(5000)
        if slow := slow_frames():
            self.context.add_init_script(slow)
        self.context.route("**/*", self.respond)
        self.page = self.context.new_page()

    def settle(self):
        """Wait until the page has drawn what it just did and nothing is moving."""

        self.page.evaluate(_PAINTED)
        self.page.wait_for_function(_STILL)

    def holds(self, route):
        """Whether a script is being held back, and so not answered yet."""

        if self.held is None or route.request.resource_type != "script":
            return False
        self.held.append(route)
        return True

    def outcomes(self, probes):
        """What each of ``probes`` returned or threw, in one round trip:
        ``[(True, value) | (False, error), ...]``."""

        if not probes:
            return []
        sources, args = zip(*probes, strict=True)
        return [tuple(outcome) for outcome in self.page.evaluate(_in_one_trip(sources), list(args))]

    def read(self, probes):
        """What each of ``probes`` returned, in one round trip."""

        return [taken(outcome) for outcome in self.outcomes(probes)]

    def respond(self, route):
        if not self.holds(route):
            self.answer(route)

    def answer(self, route):
        path = urlsplit(route.request.url).path
        name = path.strip("/")
        if name in self.pages:
            route.fulfill(content_type=mimetypes.guess_type(name)[0] or "text/html", body=self.pages[name])
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
        return super().run(result if result is None else _Watched(result, self))

    @override
    def __getstate__(self):
        # A parallel worker sends a failed subtest to the parent with its test,
        # once the test has finished, when Django no longer leaves out what
        # cannot be pickled: the browser it drove and its cleanups stay here.
        return {key: value for key, value in super().__getstate__().items() if is_pickable(value)}

    def shoot(self, name):
        """The page as it stands, saved as ``name``.png in this test's own
        hq-browser-failure-* folder in the temporary directory, every one of
        which CI keeps when a gate fails."""

        if self.failures is None:
            self.failures = Path(tempfile.mkdtemp(prefix=f"hq-browser-failure-{self._testMethodName}-"))
        path = self.failures / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        print(f"Browser failure screenshot: {path}")

    def subtest_failed(self, params):
        """A failed subtest's page, shot while it is still in the browser and
        named for its page, width and rule. A browser that has gone with the
        failure is said instead."""

        name = failure_name(params)
        try:
            self.shoot(name)
        except BrowserError as gone:
            print(f"No browser failure screenshot of {name}: {str(gone).splitlines()[0]}")

    def capture_failure(self):
        """A test that failed outside its subtests, shot as it ended."""

        if self.failed:
            self.shoot("page")

    def open(self, name, width):
        self.page.set_viewport_size(viewport(width))
        self.page.goto(f"{ORIGIN}/{name}/", wait_until="load")
        self.page.evaluate(_FONTS_READY)

    def moved_once_drawn(self, url, width, settled):
        """What moves on ``url`` after it is first drawn.

        The page is drawn with its deferred scripts held back, which is what a
        reader sees before they run, and every box that shows is noted. A
        script the parser waits for is part of that drawing, so it is let
        through. Then the rest run, ``settled`` waits for what they start, and
        every element that stands somewhere else is named.
        """

        self.held = []
        self.page.set_viewport_size(viewport(width))
        self.page.goto(url, wait_until="commit")
        for _frame in range(600):
            parsing = self.page.evaluate(_UNTIL_DRAWN)
            if parsing is None:
                break
            waiting = [route for route in self.held if route.request.url in set(parsing)]
            for route in waiting:
                self.held.remove(route)
                self.answer(route)
            # A parser stopped in the head has no frame to draw, so it is
            # asked again at once rather than waited on for one.
            if not waiting and not self.page.evaluate(_IN_THE_HEAD):
                self.page.evaluate(_SETTLED)
        else:
            raise AssertionError(f"{url} at {width}px was never drawn")
        held, self.held = self.held, None
        for route in held:
            self.answer(route)
        settled()
        return self.page.evaluate(_MOVED_SINCE_DRAWN)

    def geometry(self, path, width):
        """What the layout invariants find on the page as loaded, by check:
        every one the gates hold on a page as a reader has it."""

        return self.checked(geometry_probes(path, width))

    def scheme(self):
        """The palette the system asks the page for."""

        return "dark" if self.page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches") else "light"

    def lost_fills(self):
        """The fills one palette shows and the other loses, read in each on
        this load; the page is left in the palette it was in."""

        was = self.scheme()
        steps = {}
        for palette in ("light", "dark"):
            self.page.emulate_media(color_scheme=palette)
            self.page.evaluate(_FINISH_TRANSITIONS)
            steps[palette] = self.page.evaluate(_FILL_STEPS, [SELECTORS["contrast_scope"], SELECTORS["vendored"]])
        self.page.emulate_media(color_scheme=was)
        self.page.evaluate(_FINISH_TRANSITIONS)
        return lost_fills(steps, LINE_CONTRAST)

    def palette(self):
        """What depends on the palette: frames drawn by a border, text, lines
        and fills against what they sit on."""

        return self.checked(_PALETTE)

    def findings(self, path, width):
        """What the layout rules find in HQ's own markup, checks that found
        nothing left out: the geometry and the frames in the palette loaded.
        The vendored markup is stood in for while they read, and put back
        however they end: each probe runs on its own."""

        found = self.checked(
            {
                "stand in": (_STAND_IN, SELECTORS["vendored"]),
                **geometry_probes(path, width),
                **_PALETTE,
                "put back": (_PUT_BACK, None),
            }
        )
        found["fills"] = self.lost_fills()
        return {check: said for check, said in found.items() if said}

    def checked(self, probes):
        """Each check's findings, read in one round trip; a probe that threw
        is a finding of its own check, so it hides no other."""

        outcomes = self.outcomes(list(probes.values()))
        return {
            check: _AS_FINDINGS.get(check, _as_is)(value) if returned else [f"the probe threw: {value}"]
            for check, (returned, value) in zip(probes, outcomes, strict=True)
        }
