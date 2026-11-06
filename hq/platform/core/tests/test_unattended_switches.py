"""What HQ may do unattended is decided by the deployment, never by a literal in settings.

Each switch that lets HQ act with nobody asking, such as acting on a finding on
its own, is read from the environment under its own name and is off unless set.
The loading cases run in a fresh process, since settings are read once at
import.
"""

import ast
import json
import os
import subprocess
import sys

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.test_settings_defaults import OVERRIDES, REQUIRED
from hq.platform.core.tests.test_settings_env import REPO_ROOT, SETTINGS, subprocess_env

# HQ queuing repairs with nobody asking.
UNATTENDED = ("SEVERINO_FINDINGS_AUTO_REMEDY",)


def is_switch(name: str) -> bool:
    return name in UNATTENDED


def switch_assignments() -> dict[str, ast.expr]:
    """Each switch settings.py assigns at module level, with the expression it is given."""

    tree = ast.parse(SETTINGS.read_text(encoding="utf-8"))
    return {
        target.id: statement.value
        for statement in tree.body
        if isinstance(statement, ast.Assign | ast.AnnAssign) and statement.value is not None
        for target in (statement.targets if isinstance(statement, ast.Assign) else [statement.target])
        if isinstance(target, ast.Name) and is_switch(target.id)
    }


def reads_itself_off_by_default(name: str, value: ast.expr) -> bool:
    """Whether the value is `env_bool("<name>")`, off unless the deployment sets it."""

    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "env_bool"
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Constant)
        and value.args[0].value == name
        and all(
            keyword.arg == "default" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
            for keyword in value.keywords
        )
    )


# Prints every switch the loaded settings hold, found by name, so one added later is covered.
PROBE = (
    "import json; from hq.config import settings as s; "
    "from hq.platform.core.tests.test_unattended_switches import is_switch; "
    "print(json.dumps({name: getattr(s, name) for name in dir(s) if is_switch(name)}))"
)


def switches_when(**env: str) -> dict[str, object]:
    inherited = tuple(name for name in os.environ if is_switch(name))
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=subprocess_env(drop=OVERRIDES + inherited, **REQUIRED, **env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


@tag("source")
class SwitchDeclarationTests(SimpleTestCase):
    def test_every_switch_is_read_from_the_environment_under_its_own_name(self):
        assigned = switch_assignments()

        self.assertIn("SEVERINO_FINDINGS_AUTO_REMEDY", assigned)
        self.assertEqual(
            sorted(name for name, value in assigned.items() if not reads_itself_off_by_default(name, value)), []
        )

    def test_a_literal_or_a_borrowed_name_is_caught(self):
        """The negative control: a check that read nothing would pass the test above."""

        cases = {
            "SEVERINO_EXAMPLE_SWITCH = False": False,
            "SEVERINO_EXAMPLE_SWITCH = env_bool('SEVERINO_OTHER_SWITCH')": False,
            "SEVERINO_EXAMPLE_SWITCH = env_bool('SEVERINO_EXAMPLE_SWITCH', default=True)": False,
            "SEVERINO_EXAMPLE_SWITCH = env_bool('SEVERINO_EXAMPLE_SWITCH')": True,
        }
        for source, holds in cases.items():
            with self.subTest(source):
                statement = ast.parse(source).body[0]
                self.assertEqual(reads_itself_off_by_default("SEVERINO_EXAMPLE_SWITCH", statement.value), holds)


class SwitchLoadingTests(SimpleTestCase):
    def test_each_switch_is_off_unless_the_deployment_turns_it_on(self):
        unset = switches_when()

        self.assertIn("SEVERINO_FINDINGS_AUTO_REMEDY", unset)
        self.assertEqual(unset, dict.fromkeys(unset, False))
        self.assertEqual(switches_when(**dict.fromkeys(unset, "1")), dict.fromkeys(unset, True))
