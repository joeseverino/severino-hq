"""What DJANGO_DEBUG alone decides: production's defaults with it off, development's with it on.

Each mode is loaded in a fresh process, because settings are read once at
import and the suite's own process is configured for tests.
"""

import json
import subprocess
import sys

from django.test import SimpleTestCase

from hq.platform.core.tests.test_settings_env import REPO_ROOT, subprocess_env

# Every switch a default below can be overridden by, so the inherited
# environment cannot decide what is being measured.
OVERRIDES = (
    "DJANGO_DEBUG",
    "DJANGO_SECRET_KEY",
    "DJANGO_SECRET_KEY_FILE",
    "DJANGO_ALLOWED_HOSTS",
    "DJANGO_SESSION_COOKIE_SECURE",
    "DJANGO_CSRF_COOKIE_SECURE",
    "DJANGO_TEMPLATE_CACHE",
    "DJANGO_WHITENOISE_AUTOREFRESH",
    "SEVERINO_APP_ENV_PATH",
)
NAMES = (
    "DEBUG",
    "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE",
    "TEMPLATE_CACHE",
    "STATIC_LIVE",
    "ALLOWED_HOSTS",
)
PROBE = (
    "import json; from hq.config import settings as s; "
    f"print(json.dumps({{name: getattr(s, name) for name in {NAMES!r}}}))"
)


def load(**env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=subprocess_env(drop=OVERRIDES, **env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def settings_of(**env: str) -> dict:
    result = load(**env)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class DebugDefaultsTests(SimpleTestCase):
    def test_debug_decides_each_default(self):
        production = settings_of(DJANGO_SECRET_KEY="defaults-probe-key-0123456789abcdef0123456789")
        development = settings_of(DJANGO_DEBUG="1")
        expected = {
            "DEBUG": (False, True),
            "SESSION_COOKIE_SECURE": (True, False),
            "CSRF_COOKIE_SECURE": (True, False),
            "TEMPLATE_CACHE": (True, False),
            "STATIC_LIVE": (False, True),
        }
        for name, (off, on) in expected.items():
            with self.subTest(name):
                self.assertEqual((production[name], development[name]), (off, on))
        with self.subTest("ALLOWED_HOSTS"):
            self.assertEqual(production["ALLOWED_HOSTS"], [])
            self.assertTrue(development["ALLOWED_HOSTS"])
            self.assertTrue(all(host in {"localhost", "127.0.0.1"} for host in development["ALLOWED_HOSTS"]))

    def test_production_refuses_to_start_without_a_signing_key(self):
        result = load()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
