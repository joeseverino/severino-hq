"""The 1Password-rendered app env file must work for ANY container process.

The regression this pins: env_file was removed from compose, and sourcing the
mounted env only in the entrypoint left `docker compose exec` processes
(hq sync / shell / superuser) without DJANGO_SECRET_KEY. settings.py now loads
the file itself, so a bare `python -c "from config import settings"` — the
shape of every exec'd management command — must succeed with no secrets in
its inherited environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent

# Names that switch the extension set on. Cleared from every subprocess below,
# because these tests are about one thing -- whether settings can read the
# mounted env file -- and inherit the caller's environment to prove it works
# from a bare process.
#
# Left inherited, they made this file fail whenever it was run with extensions
# installed, which is precisely the pass that is meant to catch problems before
# a merge. A subprocess here starts with no DJANGO_DEBUG, so admission switches
# itself on, looks for the signed lock a composed image would have supplied, and
# refuses to start -- a true statement about a situation none of these tests are
# describing. It is the failure mode `scripts/check.sh` warns about in its own
# comments: a host test that quietly assumed nothing was installed.
PLUGIN_ENV = (
    "SEVERINO_HQ_PLUGINS",
    "SEVERINO_HQ_PLUGIN_LOCK",
    "SEVERINO_HQ_PLUGIN_POLICY_SHA256",
    "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION",
)


def subprocess_env(*, drop: tuple[str, ...] = (), **overrides: str) -> dict[str, str]:
    """The caller's environment, minus the extension set, plus overrides."""

    removed = set(PLUGIN_ENV) | set(drop)
    env = {
        key: value for key, value in os.environ.items() if key not in removed
    }
    env.update(overrides)
    return env


class MountedAppEnvTests(SimpleTestCase):
    def test_settings_load_shell_quoted_env_file(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(
                "DJANGO_SECRET_KEY='file-secret-key-0123456789abcdef'\n"
                "SEVERINO_SITE_NAME='Severino HQ'\n"
                "DJANGO_ALLOWED_HOSTS='hq.example.com'\n"
            )
            env_path = fh.name
        self.addCleanup(os.unlink, env_path)

        env = subprocess_env(
            drop=(
                "DJANGO_SECRET_KEY",
                "DJANGO_DEBUG",
                "DJANGO_ALLOWED_HOSTS",
                "SEVERINO_SITE_NAME",
            ),
            SEVERINO_APP_ENV_PATH=env_path,
        )

        script = "; ".join(
            [
                "from config import settings",
                "print(settings.SECRET_KEY)",
                "print(','.join(settings.ALLOWED_HOSTS))",
            ]
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[0], "file-secret-key-0123456789abcdef")
        self.assertIn("hq.example.com", lines[-1])

    def test_real_environment_wins_over_file(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("DJANGO_SECRET_KEY='from-file'\n")
            env_path = fh.name
        self.addCleanup(os.unlink, env_path)

        env = subprocess_env(
            SEVERINO_APP_ENV_PATH=env_path,
            DJANGO_SECRET_KEY="from-real-environment",
        )

        result = subprocess.run(
            [sys.executable, "-c", "from config import settings; print(settings.SECRET_KEY)"],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "from-real-environment")

    def test_fiscal_start_month_must_be_valid(self):
        env = subprocess_env(
            DJANGO_SECRET_KEY="test-secret",
            SEVERINO_FISCAL_YEAR_START_MONTH="13",
        )

        result = subprocess.run(
            [sys.executable, "-c", "from config import settings"],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "SEVERINO_FISCAL_YEAR_START_MONTH must be between 1 and 12",
            result.stderr,
        )

    def test_doc_review_interval_must_be_positive(self):
        env = subprocess_env(
            DJANGO_SECRET_KEY="test-secret",
            SEVERINO_DOC_REVIEW_INTERVAL_DAYS="0",
        )

        result = subprocess.run(
            [sys.executable, "-c", "from config import settings"],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "SEVERINO_DOC_REVIEW_INTERVAL_DAYS must be at least 1",
            result.stderr,
        )
