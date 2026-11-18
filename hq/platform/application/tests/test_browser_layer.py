"""The browser layer's claim, held to the policy HQ sends."""

from django.conf import settings
from django.test import TestCase
from django.utils.csp import CSP

from hq.platform.application.request_standard import _browser
from hq.platform.application.standards import Standing


def sources_of(response, wanted):
    directives = (directive.split() for directive in response[CSP.HEADER_ENFORCE].split(";"))
    return next(sources for name, *sources in directives if name == wanted)


class BrowserLayerTests(TestCase):
    def test_the_claim_holds_and_a_page_is_sent_a_script_src_of_its_nonce(self):
        sources = sources_of(self.client.get(settings.LOGIN_URL), "script-src")

        self.assertIs(_browser(None).standing, Standing.MET)
        self.assertTrue(any(source.startswith("'nonce-") for source in sources), sources)
        self.assertIn(CSP.STRICT_DYNAMIC, sources)
        self.assertNotIn(CSP.UNSAFE_INLINE, sources)

    def test_a_page_is_sent_style_elements_of_its_own_origin_alone(self):
        """No inline style element, so selectors injected into a page cannot
        read it; a page's sheets are HQ's own."""

        self.assertEqual(sources_of(self.client.get(settings.LOGIN_URL), "style-src-elem"), [CSP.SELF])
