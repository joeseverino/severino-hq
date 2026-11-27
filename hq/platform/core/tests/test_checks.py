"""Deployment checks judge settings, and run only when a deployment is checked."""

from django.test import SimpleTestCase, override_settings

from hq.platform.core import checks


class StaticLiveNeedsDebugTests(SimpleTestCase):
    def test_static_live_without_debug_is_an_error(self):
        with override_settings(STATIC_LIVE=True, DEBUG=False):
            errors = checks.static_live_needs_debug()
        self.assertEqual(len(errors), 1)

    def test_static_live_with_debug_or_off_passes(self):
        for live, debug in ((True, True), (False, False), (False, True)):
            with self.subTest(live=live, debug=debug), override_settings(STATIC_LIVE=live, DEBUG=debug):
                self.assertEqual(checks.static_live_needs_debug(), [])
