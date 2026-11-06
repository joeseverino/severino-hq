"""What a server process loads before its first request.

A module loaded at start is memory every process holds, idle or not; a module
first needed by one kind of request loads on that request.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.facts import imports, scan
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


# A name a class-body field's annotation reads, with the class and the field:
# an ast.Name, so neither an attribute's own name nor a keyword's.
ANNOTATION_NAME = {
    "kind": "identifier",
    "not": {
        "any": [
            {"inside": {"kind": "attribute", "field": "attribute"}},
            {"inside": {"kind": "keyword_argument", "field": "name"}},
        ]
    },
    "inside": {
        "stopBy": "end",
        "kind": "type",
        "inside": {
            "field": "type",
            "kind": "assignment",
            "has": {"field": "left", "pattern": "$FIELD"},
            "inside": {
                "kind": "expression_statement",
                "inside": {
                    "kind": "block",
                    "inside": {
                        "field": "body",
                        "kind": "class_definition",
                        "has": {"field": "name", "pattern": "$CLASS"},
                    },
                },
            },
        },
    },
}


def lazy_names_in_class_annotations(*paths: Path, root: Path = REPO_ROOT) -> list[str]:
    """Each class-body annotation under ``paths`` that names one of its module's lazy imports, as ``path:line Class.field: Name``.

    A dataclass, a pydantic model or a NamedTuple reads its annotations' values
    as the class is made, and a base in another module can make any class one,
    so such a name loads its lazy import with the module that declares it.
    """

    lazy: dict[Path, set[str]] = {}
    for each in imports(*paths):
        if each.top and "lazy" in each.waits:
            lazy.setdefault(each.path, set()).update(each.bound())
    return sorted(
        f"{named.path.relative_to(root)}:{named.line} {named.captured['CLASS']}.{named.captured['FIELD']}: {named.text}"
        for named in scan({"named": ANNOTATION_NAME}, *paths)
        if named.text in lazy.get(named.path, ())
    )


@tag("source", "tree")
class LazyImportAnnotationTests(SimpleTestCase):
    def test_no_class_annotation_names_a_lazy_import(self):
        self.assertEqual(
            lazy_names_in_class_annotations(*(REPO_ROOT / root for root in PYTHON_ROOTS)),
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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "planted.py").write_text(planted)

            found = lazy_names_in_class_annotations(root, root=root)

        self.assertEqual(found, ["planted.py:10 Mention.kind: example", "planted.py:9 Mention.link: Link"])
