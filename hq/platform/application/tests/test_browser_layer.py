"""The browser layer's claim, held to the policy HQ sends."""

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils.csp import CSP

from hq.platform.application.request_layers import _browser_layer


def script_sources(response):
    directives = (directive.split() for directive in response[CSP.HEADER_ENFORCE].split(";"))
    return next(sources for name, *sources in directives if name == "script-src")


class BrowserLayerTests(TestCase):
    def test_the_claim_holds_and_a_page_is_sent_a_script_src_of_its_nonce(self):
        sources = script_sources(self.client.get(settings.LOGIN_URL))

        self.assertTrue(_browser_layer().holds)
        self.assertTrue(any(source.startswith("'nonce-") for source in sources), sources)
        self.assertIn(CSP.STRICT_DYNAMIC, sources)
        self.assertNotIn(CSP.UNSAFE_INLINE, sources)

    def test_a_script_src_that_trusts_an_origin_or_inline_script_does_not_hold(self):
        for scripts in ([CSP.SELF, CSP.NONCE], [CSP.NONCE, CSP.STRICT_DYNAMIC, CSP.UNSAFE_INLINE]):
            with (
                self.subTest(scripts=scripts),
                override_settings(SECURE_CSP={**settings.SECURE_CSP, "script-src": scripts}),
            ):
                layer = _browser_layer()
                self.assertFalse(layer.holds)
                self.assertIn("scripts by origin", layer.evidence)
