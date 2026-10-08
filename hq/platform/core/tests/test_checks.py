"""Deployment checks judge settings, and run only when a deployment is checked."""

from django.test import SimpleTestCase, override_settings

from hq.platform.core import checks


class StaticLiveNeedsDebugTests(SimpleTestCase):
    def test_static_live_without_debug_is_an_error(self):
        with override_settings(STATIC_LIVE=True, DEBUG=False):
            errors = checks.static_live_needs_debug()
        self.assertEqual([error.id for error in errors], ["hq.E110"])

    def test_static_live_with_debug_or_off_passes(self):
        for live, debug in ((True, True), (False, False), (False, True)):
            with self.subTest(live=live, debug=debug), override_settings(STATIC_LIVE=live, DEBUG=debug):
                self.assertEqual(checks.static_live_needs_debug(), [])


class StaticLiveIsADeployCheckTests(SimpleTestCase):
    def test_it_runs_only_with_deploy_checks(self):
        from django.core.checks import registry

        self.assertNotIn(
            checks.static_live_needs_debug,
            registry.registry.get_checks(include_deployment_checks=False),
        )
        self.assertIn(
            checks.static_live_needs_debug,
            registry.registry.get_checks(include_deployment_checks=True),
        )
