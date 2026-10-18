"""The packages HQ defers are loaded by their first use, never by a start."""

import importlib.util
import json
import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase

from hq.config.first_use import FIRST_USE

# What only sign-in, a bearer token or a migration uses: a start loads none of
# them. The MCP stack is built as the application starts, so it is not here.
FIRST_USE_ONLY = {"requests", "urllib3", "charset_normalizer", "certifi", "jwt", "cryptography", "sqlparse"}

# The web application as uvicorn loads it, then what of the deferred set it loaded.
_START = """
import json, sys
import hq.config.asgi  # noqa: F401
from hq.config.first_use import FIRST_USE
print(json.dumps(sorted({name.partition(".")[0] for name in sys.modules} & FIRST_USE)))
"""


# Every deferred package, used once it has been deferred: its own import-time
# work must run against real modules, never against its lazy placeholders.
_USE = """
import importlib
from hq.config.first_use import FIRST_USE, load_on_first_use
load_on_first_use()
for name in sorted(FIRST_USE):
    vars(importlib.import_module(name))
import httpx2
httpx2.Client().close()
print("used")
"""


class FirstUseTests(SimpleTestCase):
    def test_a_deferred_package_imports_its_own_modules_eagerly(self):
        used = subprocess.run(
            [sys.executable, "-c", _USE],
            cwd=settings.BASE_DIR,
            env={**os.environ, "DJANGO_SETTINGS_MODULE": "hq.config.settings"},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(used.returncode, 0, used.stderr)
        self.assertEqual(used.stdout.strip().splitlines()[-1], "used")

    def test_every_deferred_name_is_an_installed_package(self):
        # A name that matches nothing defers nothing, and says so here.
        self.assertEqual(sorted(name for name in FIRST_USE if importlib.util.find_spec(name) is None), [])

    def test_starting_the_web_application_loads_nothing_only_sign_in_or_migrations_use(self):
        environment = {**os.environ, "DJANGO_SETTINGS_MODULE": "hq.config.settings"}
        started = subprocess.run(
            [sys.executable, "-c", _START],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(set(json.loads(started.stdout.strip().splitlines()[-1])) & FIRST_USE_ONLY, set())
