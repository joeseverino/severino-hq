"""What HQ may do unattended is decided by the deployment, never by a literal in settings.

Each switch that lets HQ act with nobody asking, such as acting on a finding on
its own, is read from the environment under its own name and is off unless set,
so a literal, a borrowed name or a default of on fails one half of the test.
Each case runs in a fresh process, since settings are read once at import.
"""

import json
import subprocess
import sys

from django.test import SimpleTestCase

from hq.platform.core.tests.test_settings_defaults import OVERRIDES, REQUIRED
from hq.platform.core.tests.test_settings_env import REPO_ROOT, subprocess_env

# HQ queuing repairs with nobody asking.
UNATTENDED = ("SEVERINO_FINDINGS_AUTO_REMEDY",)
PROBE = (
    "import json; from hq.config import settings as s; "
    f"print(json.dumps({{name: getattr(s, name) for name in {UNATTENDED!r}}}))"
)


def switches_when(**env: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=subprocess_env(drop=OVERRIDES + UNATTENDED, **REQUIRED, **env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class SwitchLoadingTests(SimpleTestCase):
    def test_each_switch_is_off_unless_the_deployment_turns_it_on(self):
        self.assertEqual(switches_when(), dict.fromkeys(UNATTENDED, False))
        self.assertEqual(switches_when(**dict.fromkeys(UNATTENDED, "1")), dict.fromkeys(UNATTENDED, True))
