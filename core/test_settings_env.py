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

        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "DJANGO_SECRET_KEY",
                "DJANGO_DEBUG",
                "DJANGO_ALLOWED_HOSTS",
                "SEVERINO_SITE_NAME",
            }
        }
        env["SEVERINO_APP_ENV_PATH"] = env_path

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from config import settings;"
                "print(settings.SECRET_KEY);"
                "print(settings.SITE_NAME if hasattr(settings, 'SITE_NAME') else '');"
                "print(','.join(settings.ALLOWED_HOSTS))",
            ],
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

        env = dict(os.environ)
        env["SEVERINO_APP_ENV_PATH"] = env_path
        env["DJANGO_SECRET_KEY"] = "from-real-environment"

        result = subprocess.run(
            [sys.executable, "-c", "from config import settings; print(settings.SECRET_KEY)"],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "from-real-environment")
