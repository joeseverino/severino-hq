"""The packages HQ defers are loaded by their first use, never by a start."""

import importlib.util
import json
import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase

from hq.config.first_use import FIRST_USE

# What a start does load of the deferred set: the MCP server is built as the
# application is, for its sessions to run for the application's lifetime, and
# these are what building it touches. Everything else waits for its first use.
STARTED_BY_MCP = {"attr", "attrs", "idna", "jsonschema", "mcp", "mcp_types", "python_multipart", "referencing", "rpds"}

# The web application as uvicorn loads it, then what of the deferred set it loaded.
_START = """
import json, sys
import hq.config.asgi  # noqa: F401
from hq.config.first_use import FIRST_USE
print(json.dumps(sorted({name.partition(".")[0] for name in sys.modules} & FIRST_USE)))
"""


class FirstUseTests(SimpleTestCase):
    def test_every_deferred_name_is_an_installed_package(self):
        # A name that matches nothing defers nothing, and says so here.
        self.assertEqual(sorted(name for name in FIRST_USE if importlib.util.find_spec(name) is None), [])

    def test_starting_the_web_application_loads_only_what_the_mcp_server_is_built_from(self):
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
        self.assertEqual(set(json.loads(started.stdout.strip().splitlines()[-1])), STARTED_BY_MCP)
