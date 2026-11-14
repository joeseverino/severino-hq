"""The WebKit defect that holds HQ's policy open to style attributes, watched
until it is fixed."""

from typing import override

from django.conf import settings
from django.utils.csp import CSP

from hq.platform.core.tests.browser.estate import is_report
from hq.platform.core.tests.browser.gate import BrowserGate, sent
from hq.platform.core.tests.browser.serving import ORIGIN

_SELECT = "/example-select/"
_A_SELECT = (
    '<!doctype html><html><head><meta charset="utf-8"></head><body>'
    '<select aria-label="Example"><option>Example</option></select></body></html>'
)
_HELD_OPEN = f"style-src-attr {CSP.UNSAFE_INLINE}"


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
