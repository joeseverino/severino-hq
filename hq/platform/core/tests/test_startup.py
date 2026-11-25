"""What a server process loads before its first request.

A module loaded at start is memory every process holds, idle or not; a module
first needed by one kind of request loads on that request.
"""

import json
import os
import subprocess
import sys

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.test_settings_env import REPO_ROOT, subprocess_env

# How many modules each start loads, the host alone as production starts it
# (DEBUG off, imports lazy): about a tenth above what it loads today, a ceiling
# a new eager import of a library or a domain crosses. The web process's first
# request loads the URLconf and what its views import; a management command
# that saves nothing never needs them.
WEB_BUDGET = 570
SETUP_BUDGET = 500
# Loaded by the first MCP request, never at start.
ON_FIRST_MCP_REQUEST = ("mcp", "mcp_types", "opentelemetry", "sse_starlette")
# Loaded by what validates a command or reads a provider, never by django.setup.
AFTER_SETUP = ("pydantic", "pydantic_core")
PROBE = "import json, sys; {start}; print(json.dumps(sorted(sys.modules)))"


def started_modules(start: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(start=start)],
        cwd=REPO_ROOT,
        env=subprocess_env(
            drop=("DJANGO_DEBUG", "SEVERINO_DATABASE_PATH"),
            DJANGO_SECRET_KEY="startup-probe-key-not-a-real-secret-only-for-this-test",
            SEVERINO_DATABASE_PATH=os.devnull,
            DJANGO_SETTINGS_MODULE="hq.config.settings",
        ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


# A start loads the shipped code, never a test, and what the lock installs.
@tag("tree")
class StartupBudgetTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The ASGI module switches lazy imports on itself; manage.py does before django.setup.
        cls.web = started_modules("import hq.config.asgi")
        cls.setup = started_modules("from hq.config.imports import lazily; lazily(); import django; django.setup()")

    def test_the_web_process_starts_without_the_mcp_sdk(self):
        loaded = sorted({name.partition(".")[0] for name in self.web} & set(ON_FIRST_MCP_REQUEST))
        self.assertEqual(loaded, [], "loaded at start by an eager import; reach it lazily (`lazy from`)")

    def test_django_setup_loads_no_validation_library(self):
        loaded = sorted({name.partition(".")[0] for name in self.setup} & set(AFTER_SETUP))
        self.assertEqual(loaded, [], "an app's ready(), a model or a check reaches it eagerly; reach it lazily")

    def test_the_web_process_starts_within_its_module_budget(self):
        self.assertLessEqual(len(self.web), WEB_BUDGET, "reach what one kind of request needs lazily (`lazy from`)")

    def test_django_setup_stays_within_its_module_budget(self):
        """What every management command and the container's boot steps load before they do anything."""

        self.assertLessEqual(len(self.setup), SETUP_BUDGET, "an app's ready() reaches it eagerly; reach it lazily")
