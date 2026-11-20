"""What the registry declares reaches the controller's contracts by derivation.

Each fact has one owner. The registry's are built into schemas every time
(``bridge_registry``), the bridge's own are written in ``bridge_base.json``,
and the files the controller's generator and the secret renderer read are
written from the two. These hold the join and what the registry refuses to
emit.
"""

import json
import subprocess
import sys
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, tag

from hq.domains.control_plane import bridge_contract, bridge_registry
from hq.domains.control_plane.provider_adapters import caddy
from hq.platform.core.tests.test_settings_env import REPO_ROOT, subprocess_env


def _base() -> dict:
    return json.loads(bridge_contract.BASE_PATH.read_text(encoding="utf-8"))


@tag("source")
class CheckCommandTests(SimpleTestCase):
    def test_the_command_reports_drift_and_writes_nothing_when_checking(self):
        derived = bridge_contract.emitted()
        stale = {path: text + " " for path, text in derived.items()}
        with (
            patch.object(bridge_contract, "emitted", return_value=stale),
            self.assertRaises(CommandError) as raised,
        ):
            call_command("bridge_contract", "--check", stdout=StringIO(), stderr=StringIO())
        self.assertIn("behind the registry", str(raised.exception))
        for path, text in derived.items():
            self.assertEqual(path.read_text(encoding="utf-8"), text)
        out = StringIO()
        call_command("bridge_contract", "--check", stdout=out)
        self.assertIn("current", out.getvalue())


class JoinTests(SimpleTestCase):
    def test_a_name_stated_twice_is_refused(self):
        base = _base()
        name = next(iter(base["components"]["schemas"]))
        with self.assertRaisesMessage(ValueError, "restates"):
            bridge_contract.joined(base, {name: {"type": "string"}})

    def test_a_reference_to_nothing_is_refused(self):
        with self.assertRaisesMessage(ValueError, "nothing declares"):
            bridge_contract.joined(_base(), {})

    def test_two_declarations_under_one_name_are_refused(self):
        with (
            patch.object(caddy, "SHARED", (*caddy.SHARED, caddy.SHARED[0])),
            self.assertRaisesMessage(ValueError, "Two declarations"),
        ):
            bridge_registry.schemas()

    def test_two_values_under_one_constant_are_refused(self):
        with self.assertRaisesMessage(ValueError, "its own controller constant"):
            bridge_registry._enumeration("Example", ("example.a_b", "example_a.b"), "Example")


class CleanTests(SimpleTestCase):
    def test_a_nullable_value_states_one_type(self):
        found = bridge_registry._clean(
            {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}], "title": "N"}, {}
        )
        self.assertEqual(found, {"type": ["integer", "null"], "minimum": 0})

    def test_a_property_called_title_is_kept(self):
        found = bridge_registry._clean({"properties": {"title": {"type": "string", "title": "T"}}}, {})
        self.assertEqual(found, {"properties": {"title": {"type": "string"}}})


def _production(**env: str) -> subprocess.CompletedProcess:
    """Settings loaded as production loads them, from ``env`` and nothing the caller set.

    Prints each required variable's setting, by the name settings give it.
    """

    probe = "; ".join(
        [
            "import json",
            "from hq.config import settings as s",
            "print(json.dumps({name: getattr(s, name.removeprefix('DJANGO_')) for name in s.REQUIRED_APP_VARIABLES}))",
        ]
    )
    return subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=subprocess_env(
            drop=(
                "SEVERINO_APP_ENV_PATH",
                "DJANGO_CSRF_TRUSTED_ORIGINS",
                *settings.REQUIRED_APP_VARIABLES,
            ),
            DJANGO_DEBUG="0",
            **env,
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# A value for each required variable, as an application environment item holds it.
_VALUES = {
    "DJANGO_SECRET_KEY": "required-probe-key-0123456789abcdef0123456789abcdef",
    "DJANGO_ALLOWED_HOSTS": "hq.example.com",
}


@tag("source")
class RequiredAppVariablesTests(SimpleTestCase):
    """The renderer refuses an item without what production cannot run without, and nothing else."""

    def test_production_runs_on_the_required_variables_alone(self):
        """A setting production refuses to load without and the list lacks fails here."""

        loaded = _production(**_VALUES)

        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertTrue(all(json.loads(loaded.stdout.strip().splitlines()[-1]).values()))

    def test_production_cannot_serve_without_any_one_of_them(self):
        """A name on the list that production runs without fails here."""

        self.assertEqual(set(_VALUES), set(settings.REQUIRED_APP_VARIABLES))
        for name in settings.REQUIRED_APP_VARIABLES:
            with self.subTest(name=name):
                loaded = _production(**{other: value for other, value in _VALUES.items() if other != name})
                if loaded.returncode == 0:
                    # It loaded, so the setting it feeds must be empty: no
                    # allowed host answers every request 400.
                    self.assertFalse(json.loads(loaded.stdout.strip().splitlines()[-1])[name])
                else:
                    self.assertIn(name, loaded.stderr)
