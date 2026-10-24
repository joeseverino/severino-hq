"""The 1Password-rendered app env file must work for ANY container process.

settings.py loads the mounted env file itself, so every process (including
`docker compose exec`) has it, and a bare `python -c "from hq.config import settings"` (the shape of every
exec'd management command) succeeds with no secrets in its inherited
environment.
"""

import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, tag

REPO_ROOT = Path(__file__).resolve().parents[4]

# Names that switch the extension set on. Cleared from every subprocess below,
# because these tests are about one thing (whether settings can read the
# mounted env file) and inherit the caller's environment to prove it works
# from a bare process.
#
# Left inherited, they make this file fail whenever it runs with extensions
# installed, which is precisely the pass that is meant to catch problems before
# a merge. A subprocess here starts with no DJANGO_DEBUG, so admission switches
# itself on, looks for the signed lock a composed image would have supplied, and
# refuses to start: a true statement about a situation none of these tests are
# describing. It is the failure the composed suite (`mise run suite:as-image`)
# exists to catch: a host test that quietly assumes nothing is installed.
PLUGIN_ENV = (
    "SEVERINO_HQ_PLUGINS",
    "SEVERINO_HQ_PLUGIN_LOCK",
    "SEVERINO_HQ_PLUGIN_POLICY_SHA256",
    "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION",
)


def subprocess_env(*, drop: tuple[str, ...] = (), **overrides: str) -> dict[str, str]:
    """The caller's environment, minus the extension set, plus overrides."""

    removed = set(PLUGIN_ENV) | set(drop)
    env = {key: value for key, value in os.environ.items() if key not in removed}
    env.update(overrides)
    return env


class MountedAppEnvTests(SimpleTestCase):
    def test_settings_load_shell_quoted_env_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as fh:
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
                "from hq.config import settings",
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
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as fh:
            fh.write("DJANGO_SECRET_KEY='from-file'\n")
            env_path = fh.name
        self.addCleanup(os.unlink, env_path)

        env = subprocess_env(
            SEVERINO_APP_ENV_PATH=env_path,
            DJANGO_SECRET_KEY="from-real-environment",
        )

        result = subprocess.run(
            [sys.executable, "-c", "from hq.config import settings; print(settings.SECRET_KEY)"],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "from-real-environment")


class EnvIntTests(SimpleTestCase):
    NAME = "SEVERINO_TEST_ENV_INT"

    def test_a_value_that_is_not_a_number_falls_back_to_the_default(self):
        from hq.config.settings import env_int

        for raw, expected in ((None, 5), ("", 5), ("  ", 5), ("soon", 5), (" 12 ", 12), ("-3", -3)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ):
                os.environ.pop(self.NAME, None)
                if raw is not None:
                    os.environ[self.NAME] = raw
                self.assertEqual(env_int(self.NAME, 5), expected)


# ----- The configuration table ------------------------------------------------

SOURCE_ROOTS = ("hq", "hq_sdk")
SETTINGS = REPO_ROOT / "hq" / "config" / "settings.py"
TABLE = REPO_ROOT / "docs" / "Deployment.md"
TABLE_SECTION = "## Configuration"
SET_BY = {"installer", "platform", "development"}
# Settings' readers. env_secret also reads NAME_FILE.
READERS = {"env_str", "env_bool", "env_int", "env_list", "env_secret"}
# How mise hands a task its arguments; the task's --help lists them.
TASK_ARGUMENT = "usage_"


def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _key(node: ast.AST) -> ast.AST | None:
    """The name expression an environment read looks up, or None for anything else."""

    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Name) and func.id in READERS:
            return node.args[0]
        if isinstance(func, ast.Attribute) and (
            func.attr == "getenv" or (func.attr == "get" and _is_environ(func.value))
        ):
            return node.args[0]
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and _is_environ(node.value):
        return node.slice
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.In | ast.NotIn)
        and _is_environ(node.comparators[0])
    ):
        return node.left
    return None


def _key_through(node: ast.AST, forwarders: dict[str, int]) -> ast.AST | None:
    """The name a read looks up, directly or through a helper that is handed it."""

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forwarders:
        position = forwarders[node.func.id]
        return node.args[position] if len(node.args) > position else None
    return _key(node)


def _forwarded_parameter(function: ast.FunctionDef, forwarders: dict[str, int]) -> int | None:
    params = [arg.arg for arg in function.args.args]
    for node in ast.walk(function):
        key = _key_through(node, forwarders)
        if isinstance(key, ast.Name) and key.id in params:
            return params.index(key.id)
    return None


def _forwarders(tree: ast.Module) -> dict[str, int]:
    """Functions that read the variable a parameter names, and that parameter's position.

    Followed through any chain of such functions in the module, so a helper
    that takes the name as an argument hides nothing.
    """

    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    forwarders: dict[str, int] = {}
    while True:
        found = {
            function.name: position
            for function in functions
            if function.name not in forwarders and (position := _forwarded_parameter(function, forwarders)) is not None
        }
        if not found:
            return forwarders
        forwarders |= found


def names_read(sources) -> set[str]:
    """Every environment variable the given module sources read by name."""

    names = set()
    for source in sources:
        tree = ast.parse(source)
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name) and isinstance(node.value.value, str)
        }
        forwarders = _forwarders(tree)
        for node in ast.walk(tree):
            key = _key_through(node, forwarders)
            if isinstance(key, ast.Name):
                key = ast.Constant(constants.get(key.id))
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            names.add(key.value)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "env_secret":
                names.add(f"{key.value}_FILE")
    return {name for name in names if not name.startswith(TASK_ARGUMENT)}


def table_rows(markdown: str) -> list[tuple[str, str]]:
    """(variable, set by) for each name in the Configuration section's table."""

    section = markdown.split(f"\n{TABLE_SECTION}\n", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].startswith("`"):
            continue
        rows.extend((name, cells[1]) for name in re.findall(r"`([^`]+)`", cells[0]))
    return rows


def drift(read: set[str], listed: set[str]) -> dict[str, set[str]]:
    return {"read but not in the table": read - listed, "in the table but read nowhere": listed - read}


def product_sources():
    for root in SOURCE_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "tests" not in path.relative_to(REPO_ROOT).parts:
                yield path.read_text(encoding="utf-8")


@tag("source", "tree")
class ConfigurationTableTests(SimpleTestCase):
    """Every variable HQ reads is in docs/Deployment.md's table, and nothing else is."""

    def test_the_table_lists_exactly_what_hq_reads(self):
        rows = table_rows(TABLE.read_text(encoding="utf-8"))
        listed = [name for name, _ in rows]
        read = names_read(product_sources())

        self.assertIn("DJANGO_SECRET_KEY", read)
        self.assertEqual(
            drift(read, set(listed)),
            {"read but not in the table": set(), "in the table but read nowhere": set()},
        )
        self.assertEqual(sorted(name for name in listed if listed.count(name) > 1), [])
        self.assertEqual({name: who for name, who in rows if who not in SET_BY}, {})

    def test_a_variable_missing_from_the_table_or_left_in_it_fails(self):
        source = (
            "import os\n"
            "_NAMED = 'EXAMPLE_CONSTANT'\n"
            "def _read(variable):\n"
            "    return os.environ.get(variable)\n"
            "SECRET = env_secret('EXAMPLE_SECRET')\n"
            "UNLISTED = os.environ.get('EXAMPLE_UNLISTED')\n"
            "FORWARDED = _read('EXAMPLE_FORWARDED')\n"
            "PRESENT = _NAMED in os.environ\n"
            "ARGUMENT = os.environ.get('usage_example')\n"
            "os.environ.setdefault('EXAMPLE_WRITTEN', '1')\n"
        )
        table = (
            f"# Deployment\n\n{TABLE_SECTION}\n\n| Variable | Set by | Does |\n| :--- | :--- | :--- |\n"
            "| `EXAMPLE_SECRET`, `EXAMPLE_SECRET_FILE` | installer | a secret |\n"
            "| `EXAMPLE_FORWARDED` | platform | read through a helper |\n"
            "| `EXAMPLE_CONSTANT` | development | named by a constant |\n"
            "| `EXAMPLE_STALE` | installer | read by nothing |\n"
            "\n## Next\n\n| `EXAMPLE_ELSEWHERE` | installer | another section's table |\n"
        )

        found = drift(names_read([source]), {name for name, _ in table_rows(table)})

        self.assertEqual(found["read but not in the table"], {"EXAMPLE_UNLISTED"})
        self.assertEqual(found["in the table but read nowhere"], {"EXAMPLE_STALE"})


@tag("source")
class SettingsReadersTests(SimpleTestCase):
    """settings.py reads the environment only through its env_* readers."""

    def test_settings_read_the_environment_only_through_a_reader(self):
        tree = ast.parse(SETTINGS.read_text(encoding="utf-8"))
        outside = [
            node
            for statement in tree.body
            if not (isinstance(statement, ast.FunctionDef) and statement.name in READERS)
            for node in ast.walk(statement)
        ]
        # The app env loader writes defaults into the environment; it reads nothing.
        writes = {
            id(node.func.value)
            for node in outside
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "setdefault"
        }
        raw = [
            node.lineno
            for node in outside
            if (_is_environ(node) and id(node) not in writes)
            or (isinstance(node, ast.Attribute) and node.attr == "getenv")
        ]

        self.assertEqual(raw, [])
