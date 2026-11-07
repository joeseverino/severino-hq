"""What HQ's policy reports back from the navigation's pages: nothing. And the
WebKit defect that holds its style attributes open, watched until it is fixed."""

from typing import override
from urllib.parse import urlsplit

from django.conf import settings
from django.utils.csp import CSP

from hq.platform.core.tests.browser.estate import ExampleEstateGate
from hq.platform.core.tests.browser.gate import BrowserGate, sent
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.widths import WIDE

# A page the policy refuses a style element on, answered as HQ answers one,
# opened last: a browser sends a report once it gets to it, so the report this
# page draws is what the test waits for, and it comes after any the pages
# before it drew.
_REFUSED = "/example-refused/"
_STYLED = '<!doctype html><html><head><meta charset="utf-8"><style>p { color: red }</style></head><body><p>Example</p></body></html>'

_SELECT = "/example-select/"
_A_SELECT = (
    '<!doctype html><html><head><meta charset="utf-8"></head><body>'
    '<select aria-label="Example"><option>Example</option></select></body></html>'
)
_HELD_OPEN = f"style-src-attr {CSP.UNSAFE_INLINE}"


def is_report(path):
    return lambda request: urlsplit(request.url).path == path


class PolicyReportBrowserTests(ExampleEstateGate):
    """Every navigation page of the example estate, under the headers HQ sent
    it, sends ``/csp-report/`` nothing."""

    def test_no_navigation_page_sends_a_report(self):
        self.assertGreater(len(self.navigation), 10, "the navigation led almost nowhere")
        for path in self.navigation:
            self.visit(path, WIDE)
        self.served = {**self.served, _REFUSED: sent(_STYLED, {"Content-Type": "text/html", **self.policy})}
        with self.page.expect_request_finished(is_report(settings.SEVERINO_CSP_REPORT_PATH)):
            self.page.goto(f"{ORIGIN}{_REFUSED}", wait_until="load")
        # A round trip, so the page's word of the refusal has reached the gate too.
        self.settle()
        heard = self.refusals_so_far()
        report = f"{_REFUSED}: {settings.SEVERINO_CSP_REPORT_PATH} was sent"
        self.assertTrue(
            [line for line in heard if line.startswith(report)],
            f"the refused page's report never reached {settings.SEVERINO_CSP_REPORT_PATH}: {heard}",
        )
        self.assertEqual([line for line in heard if not line.startswith(_REFUSED)], [])


class WebKitSelectCanaryTests(BrowserGate):
    """WebKit 27.2 reports a style-src-attr violation for every <select>
    (https://github.com/microsoft/playwright/issues/43617), so HQ's policy
    admits style attributes. This fails once an engine stops doing so."""

    scripted = True

    @classmethod
    @override
    def render(cls):
        return {}

    def test_webkit_still_refuses_a_style_of_its_own_on_a_select(self):
        if self.browser.browser_type.name != "webkit":
            self.skipTest("WebKit's defect")
        enforced = self.policy[CSP.HEADER_ENFORCE]
        self.assertIn(_HELD_OPEN, enforced)
        tightened = {**self.policy, CSP.HEADER_ENFORCE: enforced.replace(_HELD_OPEN, f"style-src-attr {CSP.SELF}")}
        self.served = {**self.served, _SELECT: sent(_A_SELECT, {"Content-Type": "text/html", **tightened})}
        with self.page.expect_request_finished(is_report(settings.SEVERINO_CSP_REPORT_PATH)):
            self.page.goto(f"{ORIGIN}{_SELECT}", wait_until="load")
        self.settle()
        heard = self.refusals_so_far()
        self.assertTrue(
            any("style-src-attr refused" in line for line in heard),
            "WebKit no longer refuses a <select> its own style: tighten style-src-attr to 'self' in "
            f"settings.SECURE_CSP and delete this canary. Heard: {heard}",
        )
