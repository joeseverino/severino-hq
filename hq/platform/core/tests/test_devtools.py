"""The debug toolbar is a development layer and never reaches production.

Settings are loaded in a fresh process per case, because the switch is read
once at import and the suite's own settings always have it off.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import Resolver404, resolve

from hq.config.devtools import (
    DEBUG_TOOLBAR_APP,
    DEBUG_TOOLBAR_MIDDLEWARE,
    TRUSTED_TYPES_DIRECTIVES,
    debug_toolbar_enabled,
    without_trusted_types,
)
from hq.platform.core.tests.test_settings_env import REPO_ROOT, subprocess_env

INSTALLED = importlib.util.find_spec(DEBUG_TOOLBAR_APP) is not None

PROBE = "; ".join(
    [
        "import json",
        "from hq.config import settings as s",
        "print(json.dumps({"
        "'enabled': s.SEVERINO_DEBUG_TOOLBAR,"
        f"'app': {DEBUG_TOOLBAR_APP!r} in s.INSTALLED_APPS,"
        f"'middleware': {DEBUG_TOOLBAR_MIDDLEWARE!r} in s.MIDDLEWARE,"
        "'csp': sorted(s.SECURE_CSP),"
        "'internal_ips': getattr(s, 'INTERNAL_IPS', []),"
        "}))",
    ]
)


def load_settings(**env: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=subprocess_env(
            drop=("DJANGO_DEBUG", "SEVERINO_DEBUG_TOOLBAR", "SEVERINO_DEBUG_TOOLBAR_IPS"),
            DJANGO_SECRET_KEY="devtools-probe-key-0123456789abcdef0123456789abcdef",
            **env,
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class ProductionSettingsTests(SimpleTestCase):
    def assert_off(self, loaded):
        self.assertFalse(loaded["enabled"])
        self.assertFalse(loaded["app"])
        self.assertFalse(loaded["middleware"])
        self.assertLessEqual(TRUSTED_TYPES_DIRECTIVES, set(loaded["csp"]))

    def test_debug_off_never_installs_it_even_when_asked(self):
        self.assert_off(load_settings(DJANGO_DEBUG="0", SEVERINO_DEBUG_TOOLBAR="1"))

    def test_debug_on_without_the_flag_leaves_it_off(self):
        self.assert_off(load_settings(DJANGO_DEBUG="1"))

    def test_an_unrecognised_flag_value_leaves_it_off(self):
        self.assert_off(load_settings(DJANGO_DEBUG="1", SEVERINO_DEBUG_TOOLBAR="maybe"))

    def test_debug_and_the_flag_install_it_only_where_it_is_importable(self):
        loaded = load_settings(DJANGO_DEBUG="1", SEVERINO_DEBUG_TOOLBAR="1")
        if not INSTALLED:
            # The host image: asked for, and still absent.
            self.assert_off(loaded)
            return
        self.assertTrue(loaded["enabled"])
        self.assertTrue(loaded["app"])
        self.assertTrue(loaded["middleware"])
        self.assertFalse(TRUSTED_TYPES_DIRECTIVES & set(loaded["csp"]))
        self.assertIn("script-src", loaded["csp"])
        self.assertEqual(loaded["internal_ips"], ["127.0.0.1", "::1"])

    def test_the_suite_runs_without_it(self):
        self.assertFalse(settings.SEVERINO_DEBUG_TOOLBAR)
        self.assertNotIn(DEBUG_TOOLBAR_APP, settings.INSTALLED_APPS)
        self.assertNotIn(DEBUG_TOOLBAR_MIDDLEWARE, settings.MIDDLEWARE)
        with self.assertRaises(Resolver404):
            resolve("/__debug__/render_panel/")


class SwitchTests(SimpleTestCase):
    def test_every_condition_is_required(self):
        for debug in (False, True):
            for requested in (False, True):
                for testing in (False, True):
                    with self.subTest(debug=debug, requested=requested, testing=testing):
                        enabled = debug_toolbar_enabled(
                            debug=debug, requested=requested, testing=testing
                        )
                        expected = debug and requested and not testing and INSTALLED
                        self.assertEqual(enabled, expected)

    def test_only_the_trusted_types_directives_are_dropped(self):
        policy = settings.SECURE_CSP
        relaxed = without_trusted_types(policy)
        self.assertEqual(set(policy) - set(relaxed), TRUSTED_TYPES_DIRECTIVES)
        for key, value in relaxed.items():
            self.assertEqual(value, policy[key])
