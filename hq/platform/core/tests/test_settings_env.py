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

from hq.platform.application.tests.facts import TOP, Found, grouped, imports, package_of, scan

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
# describing. It is the failure the composed suite (`mise run test --composed`)
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
            check=False,
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
            check=False,
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


def _argument(position: int, key: str) -> dict:
    """A call whose argument at ``position``, a positional one, is ``key``."""

    return {
        "kind": "call",
        "has": {
            "field": "arguments",
            "has": {
                "nthChild": position + 1,
                "pattern": key,
                "not": {"any": [{"kind": kind} for kind in ("keyword_argument", "list_splat", "dictionary_splat")]},
            },
        },
    }


def _reads(key: str) -> list[dict]:
    """Each form of a read of the environment variable ``key`` names: a reader's
    or a getenv's first argument, an environ subscript that is not written, or
    a test of membership in environ."""

    environ = r"(^|\.)environ$"
    return [
        {
            "all": [
                {
                    "kind": "call",
                    "has": {
                        "field": "function",
                        "pattern": "$READER",
                        "regex": rf"^({'|'.join(sorted(READERS))})$|\.getenv$|(^|\.)environ\.get$",
                    },
                },
                _argument(0, key),
            ]
        },
        {
            "kind": "subscript",
            "all": [
                {"kind": "subscript", "has": {"field": "value", "regex": environ}},
                {"kind": "subscript", "has": {"field": "subscript", "pattern": key}},
            ],
            "not": {
                "any": [
                    {"inside": {"kind": "assignment", "field": "left"}},
                    {"inside": {"kind": "augmented_assignment", "field": "left"}},
                    {"inside": {"kind": "delete_statement", "stopBy": "end"}},
                ]
            },
        },
        {
            "kind": "comparison_operator",
            "any": [{"pattern": f"{key} in $ENVIRON"}, {"pattern": f"{key} not in $ENVIRON"}],
            "has": {"nthChild": {"position": 1, "reverse": True}, "regex": environ},
        },
    ]


# Every read, each module-level constant a key may name, and each read of the
# variable a parameter of its function names, with the function.
RULES = {
    "read": {"any": _reads("$KEY")},
    "constant": {"pattern": "$NAME = $VALUE", **TOP},
    "forwarded": {
        "any": _reads("$P"),
        "inside": {
            "stopBy": "end",
            "kind": "function_definition",
            "all": [
                {"kind": "function_definition", "has": {"field": "name", "pattern": "$NAME"}},
                {
                    "kind": "function_definition",
                    "has": {
                        "field": "parameters",
                        "pattern": "$PARAMETERS",
                        "has": {"stopBy": "end", "kind": "identifier", "pattern": "$P"},
                    },
                },
            ],
        },
    },
}


def environment_reads(*paths: Path) -> tuple[dict[str, set[Path]], list[Found]]:
    """Every environment variable the modules under ``paths``, less their
    tests, read by name, directly or through a function of their own that is
    handed it, with the modules that read it; and each read whose name none
    of them spells, so a guard over the names fails on it rather than reading
    past it. A name is spelled by a literal, a constant of the module, or a
    constant of the module it is imported from."""

    found = grouped(scan(RULES, *paths, tests=False))
    constants = {(each.path, each.captured["NAME"]): each.value("VALUE") for each in found.get("constant", [])}
    forwarders, handed = _forwarders(found.get("forwarded", []))
    reads = [
        *(
            each
            for each in found.get("read", [])
            if (each.path, each.start) not in handed and not _handed_on(each, forwarders, found.get("forwarded", []))
        ),
        *_forwarded_reads(forwarders),
    ]
    names: dict[str, set[Path]] = {}
    unnamed = []
    for each in reads:
        key = each.captured["KEY"]
        name = each.value("KEY")
        if not isinstance(name, str):
            name = constants.get((each.path, key)) if key.isidentifier() else None
        if not isinstance(name, str):
            name = _imported_constant(each.path, key)
        if not isinstance(name, str):
            unnamed.append(each)
            continue
        spelled = [name, f"{name}_FILE"] if each.captured.get("READER") == "env_secret" else [name]
        for one in spelled:
            if not one.startswith(TASK_ARGUMENT):
                names.setdefault(one, set()).add(each.path)
    return names, unnamed


def _imported_constant(path: Path, key: str) -> object:
    """The value of the constant ``key`` names through an import of ``path``'s
    (``turn.SHARE``, or ``DATABASE`` from ``from hq.config.sandbox import
    DATABASE``), or None."""

    bound, _, attribute = key.partition(".")
    if not bound.isidentifier() or (attribute and not attribute.isidentifier()) or "." in attribute:
        return None
    for each in imports(path):
        if bound not in each.bound():
            continue
        source = each.source
        if source is not None and source.startswith("."):
            if not path.is_relative_to(REPO_ROOT):
                continue
            source = each.modules(package_of(path))[0]
        for name, alias in each.names:
            if (alias or name.partition(".")[0]) != bound:
                continue
            if source is None:
                module, constant = name, attribute
            elif attribute:
                module, constant = f"{source}.{name}", attribute
            else:
                module, constant = source, name
            if not constant:
                continue
            for source in (
                REPO_ROOT / f"{module.replace('.', '/')}.py",
                REPO_ROOT / module.replace(".", "/") / "__init__.py",
            ):
                if source.is_file():
                    for found in scan({"constant": RULES["constant"]}, source):
                        if found.captured["NAME"] == constant:
                            return found.value("VALUE")
    return None


def _forwarders(forwarded) -> tuple[dict[tuple[Path, str], int], set[tuple[Path, int]]]:
    """Each function of a module's own that hands one of its parameters to a
    read, by its module and name, to that parameter's position; and where each
    such read is, which names no variable until the function is called."""

    forwarders, handed = {}, set()
    for each in forwarded:
        (signature,) = ast.parse(f"def _{each.captured['PARAMETERS']}: pass").body
        assert isinstance(signature, ast.FunctionDef)
        parameters = [argument.arg for argument in signature.args.args]
        if each.captured["P"] in parameters:
            forwarders[each.path, each.captured["NAME"]] = parameters.index(each.captured["P"])
            handed.add((each.path, each.start))
    return forwarders, handed


def _handed_on(read: Found, forwarders: dict[tuple[Path, str], int], forwarded) -> bool:
    """Whether ``read`` names its variable by an f-string of a forwarder's
    parameters (env_secret's ``f"{name}_FILE"``), which its calls spell."""

    key = read.captured["KEY"]
    fields = re.findall(r"\{([^}!:]+)", key)
    parameters = {
        each.captured["P"]
        for each in forwarded
        if each.path == read.path and (each.path, each.captured["NAME"]) in forwarders
    }
    return key[:1] in "fF" and bool(fields) and set(fields) <= parameters


def _forwarded_reads(forwarders: dict[tuple[Path, str], int]) -> list:
    """Each call of a forwarder in its own module, read as the read it forwards to."""

    if not forwarders:
        return []
    calls = {
        f"{name}@{position}": {
            **_argument(position, "$KEY"),
            "all": [{"kind": "call", "has": {"field": "function", "regex": f"^{name}$"}}],
        }
        for (_path, name), position in forwarders.items()
    }
    modules = sorted({path for path, _name in forwarders})
    return [each for each in scan(calls, *modules) if (each.path, each.rule.partition("@")[0]) in forwarders]


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


@tag("source", "tree")
class ConfigurationTableTests(SimpleTestCase):
    """Every variable HQ reads is in docs/Deployment.md's table, and nothing else is."""

    def test_the_table_lists_exactly_what_hq_reads(self):
        rows = table_rows(TABLE.read_text(encoding="utf-8"))
        listed = [name for name, _ in rows]
        read, unnamed = environment_reads(*(REPO_ROOT / root for root in SOURCE_ROOTS))

        self.assertIn("DJANGO_SECRET_KEY", read)
        self.assertEqual([f"{each.path}:{each.line} {each.text}" for each in unnamed], [])
        self.assertEqual(
            drift(set(read), set(listed)),
            {"read but not in the table": set(), "in the table but read nowhere": set()},
        )
        self.assertEqual(sorted(name for name in listed if listed.count(name) > 1), [])
        self.assertEqual({name: who for name, who in rows if who not in SET_BY}, {})

    def test_a_variable_missing_from_the_table_or_left_in_it_fails(self):
        source = (
            "import os\n"
            "from hq.config import sandbox\n"
            "from hq.config.sandbox import DATABASE\n"
            "_NAMED = 'EXAMPLE_CONSTANT'\n"
            "IMPORTED = os.environ.get(DATABASE)\n"
            "ATTRIBUTE = os.environ.get(sandbox.RUN)\n"
            "def _picked(pick):\n"
            "    chosen = pick()\n"
            "    return os.environ.get(chosen)\n"
            "def _read(variable):\n"
            "    return os.environ.get(variable)\n"
            "SECRET = env_secret('EXAMPLE_SECRET')\n"
            "UNLISTED = os.environ.get('EXAMPLE_UNLISTED')\n"
            "FORWARDED = _read('EXAMPLE_FORWARDED')\n"
            "PRESENT = _NAMED in os.environ\n"
            "ARGUMENT = os.environ.get('usage_example')\n"
            "os.environ.setdefault('EXAMPLE_WRITTEN', '1')\n"
            "INDEXED = os.environ['EXAMPLE_INDEXED']\n"
            "os.environ['EXAMPLE_SET'] = '1'\n"
            "del os.environ['EXAMPLE_DELETED']\n"
        )
        table = (
            f"# Deployment\n\n{TABLE_SECTION}\n\n| Variable | Set by | Does |\n| :--- | :--- | :--- |\n"
            "| `EXAMPLE_SECRET`, `EXAMPLE_SECRET_FILE` | installer | a secret |\n"
            "| `EXAMPLE_INDEXED` | installer | read by its key |\n"
            "| `EXAMPLE_FORWARDED` | platform | read through a helper |\n"
            "| `EXAMPLE_CONSTANT` | development | named by a constant |\n"
            "| `SEVERINO_DATABASE_PATH`, `SEVERINO_TEST_RUN` | development | named by an imported constant |\n"
            "| `EXAMPLE_STALE` | installer | read by nothing |\n"
            "\n## Next\n\n| `EXAMPLE_ELSEWHERE` | installer | another section's table |\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "example.py"
            module.write_text(source)
            read, unnamed = environment_reads(module)
            found = drift(set(read), {name for name, _ in table_rows(table)})

        self.assertEqual(found["read but not in the table"], {"EXAMPLE_UNLISTED"})
        self.assertEqual(found["in the table but read nowhere"], {"EXAMPLE_STALE"})
        self.assertEqual([each.text for each in unnamed], ["os.environ.get(chosen)"])


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
