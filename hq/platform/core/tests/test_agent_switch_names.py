"""An agent switch set under its retired name stops the start and names the new one.

Read as off, it would quietly take a grant from every agent on a deployment
that meant to keep it. Each case loads settings in a fresh process, since
settings are read once at import.
"""

import os
import subprocess
import sys

from django.test import SimpleTestCase

from hq.platform.core.tests.test_settings_defaults import OVERRIDES, REQUIRED
from hq.platform.core.tests.test_settings_env import REPO_ROOT, subprocess_env

SWITCHES = ("WRITES", "DOC_SYNC", "INFRASTRUCTURE")
# Read through getattr, so only the settings' own refusal can fail a start.
PROBE = "from hq.config import settings as s; print(getattr(s, 'SEVERINO_AGENT_ENABLE_WRITES', None))"


def load(**env: str) -> subprocess.CompletedProcess:
    inherited = tuple(
        name for name in os.environ if name.startswith(("SEVERINO_MCP_ENABLE_", "SEVERINO_AGENT_ENABLE_"))
    )
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=subprocess_env(drop=OVERRIDES + inherited, **REQUIRED, **env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class AgentSwitchNameTests(SimpleTestCase):
    def test_a_retired_switch_name_refuses_to_start_and_names_its_replacement(self):
        for switch in SWITCHES:
            with self.subTest(switch):
                result = load(**{f"SEVERINO_MCP_ENABLE_{switch}": "1"})

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"SEVERINO_AGENT_ENABLE_{switch}", result.stderr)

    def test_the_new_name_is_read(self):
        """The positive control: a check that refused every start would pass the test above."""

        result = load(SEVERINO_AGENT_ENABLE_WRITES="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "True")
