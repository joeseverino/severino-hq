"""The browser layer's claim, held to the policy HQ sends."""

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils.csp import CSP

from hq.platform.application.request_layers import _browser_layer


def sources_of(response, wanted):
    directives = (directive.split() for directive in response[CSP.HEADER_ENFORCE].split(";"))
    return next(sources for name, *sources in directives if name == wanted)


class BrowserLayerTests(TestCase):
    def test_the_claim_holds_and_a_page_is_sent_a_script_src_of_its_nonce(self):
        sources = sources_of(self.client.get(settings.LOGIN_URL), "script-src")

        self.assertTrue(_browser_layer().holds)
        self.assertTrue(any(source.startswith("'nonce-") for source in sources), sources)
        self.assertIn(CSP.STRICT_DYNAMIC, sources)
        self.assertNotIn(CSP.UNSAFE_INLINE, sources)

    def test_a_page_is_sent_style_elements_of_its_own_origin_alone(self):
        """No inline style element, so selectors injected into a page cannot
        read it; a page's sheets are HQ's own."""

        self.assertEqual(sources_of(self.client.get(settings.LOGIN_URL), "style-src-elem"), [CSP.SELF])

    def test_a_script_src_that_trusts_an_origin_or_inline_script_does_not_hold(self):
        for scripts in ([CSP.SELF, CSP.NONCE], [CSP.NONCE, CSP.STRICT_DYNAMIC, CSP.UNSAFE_INLINE]):
            with (
                self.subTest(scripts=scripts),
                override_settings(SECURE_CSP={**settings.SECURE_CSP, "script-src": scripts}),
            ):
                layer = _browser_layer()
                self.assertFalse(layer.holds)
                self.assertIn("scripts by origin", layer.evidence)
