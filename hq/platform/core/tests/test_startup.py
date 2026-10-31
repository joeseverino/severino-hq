"""What a server process loads before its first request.

A module loaded at start is memory every process holds, idle or not; a module
first needed by one kind of request loads on that request.
"""

import ast
import json
import os
import subprocess
import sys

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.test_settings_env import REPO_ROOT, subprocess_env

# How many modules each start loads, the host alone as production starts it
# (DEBUG off): about a tenth above what it loads today, a ceiling a new eager
# import of a library or a domain crosses. The web process's first request
# loads the URLconf and what its views import; a management command that saves
# nothing never needs them.
WEB_BUDGET = 980
SETUP_BUDGET = 745
# Loaded by the first MCP request, never at start.
ON_FIRST_MCP_REQUEST = ("mcp", "mcp_types", "opentelemetry", "sse_starlette")
# Loaded by what validates a command or reads a provider, never by django.setup.
AFTER_SETUP = ("pydantic", "pydantic_core")
# Every Python file of the repository: a class anywhere may be made at start.
PYTHON_ROOTS = ("hq", "hq_sdk", "demo", "tools", "tests")
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


class StartupBudgetTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.web = started_modules("import hq.config.asgi")
        cls.setup = started_modules("import django; django.setup()")

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


def lazy_names_in_class_annotations(source: str) -> list[str]:
    """Each class-body annotation that names one of its module's lazy imports, as ``line Class.field: Name``.

    A dataclass, a pydantic model or a NamedTuple reads its annotations' values
    as the class is made, and a base in another module can make any class one,
    so such a name loads its lazy import with the module that declares it.
    """

    tree = ast.parse(source)
    lazy = {
        (alias.asname or alias.name).partition(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.is_lazy
        for alias in node.names
    }
    return [
        f"{field.lineno} {cls.name}.{ast.unparse(field.target)}: {name}"
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
        for field in cls.body
        if isinstance(field, ast.AnnAssign)
        for name in sorted({node.id for node in ast.walk(field.annotation) if isinstance(node, ast.Name)} & lazy)
    ]


@tag("source", "tree")
class LazyImportAnnotationTests(SimpleTestCase):
    def test_no_class_annotation_names_a_lazy_import(self):
        found = [
            f"{path.relative_to(REPO_ROOT)}:{hit}"
            for root in PYTHON_ROOTS
            for path in sorted((REPO_ROOT / root).rglob("*.py"))
            for hit in lazy_names_in_class_annotations(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            found,
            [],
            "import an annotation-only name under `if TYPE_CHECKING:`; one also used at run time, by its module",
        )

    def test_a_planted_dataclass_field_is_found(self):
        planted = (
            "from dataclasses import dataclass\n"
            "from typing import TYPE_CHECKING\n"
            "lazy from example.links import Link\n"
            "lazy import example.kinds\n"
            "if TYPE_CHECKING:\n"
            "    from example.rows import Row\n"
            "@dataclass\n"
            "class Mention:\n"
            "    link: Link | None\n"
            "    kind: example.kinds.Kind\n"
            "    row: Row\n"
            "    def named(self) -> Link: ...\n"
        )
        self.assertEqual(lazy_names_in_class_annotations(planted), ["9 Mention.link: Link", "10 Mention.kind: example"])
