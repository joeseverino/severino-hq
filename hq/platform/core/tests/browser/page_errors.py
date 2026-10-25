"""A script that throws fails the gate, beside a policy's refusals and the console's errors."""

from typing import override

from hq.platform.core.tests.browser.gate import BrowserGate
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.serving import NONCE
from hq.platform.core.tests.browser.widths import WIDE

_PLANTED = """function plant() { throw new Error("planted"); }
plant();
"""


class PageErrorBrowserTests(BrowserGate):
    """An uncaught error is a finding of the page it was thrown on."""

    scripted = True

    @classmethod
    @override
    def render(cls):
        return {
            "planted": probe(f'<script defer src="/planted.js" nonce="{NONCE}"></script>'),
            "planted.js": _PLANTED,
        }

    def test_a_script_that_throws_fails_the_gate_naming_its_page_width_error_and_frame(self):
        self.open("planted", WIDE)
        with self.assertRaises(self.failureException) as failed:
            self.kept_the_policy()
        said = str(failed.exception)
        for part in ("/planted/", f"{WIDE}px", "Error: planted", "/planted.js:1:"):
            with self.subTest(part=part):
                self.assertIn(part, said)
