"""HQ boots with every import lazy (hq.config.imports) and is the same HQ.

A module that registers something as it loads registers nothing until a name
it binds is used, so whatever reads that registry first finds it short. A
package that binds a name one of its submodules has finds the name rebound to
the submodule once it loads.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag
from django.utils.functional import SimpleLazyObject, empty

from hq.platform.core.tests.registries import ENTRYPOINTS, PREFIX, module_of, registries
from hq.platform.core.tests.test_settings_env import REPO_ROOT
from hq.platform.core.tests.test_startup import PYTHON_ROOTS

MOVE_IT = (
    "registered only as its module loads, which a lazy import never makes happen: register it from its "
    "app's ready() or through Django's autodiscovery; a library's own goes in hq.config.imports.EAGER"
)


def booted(entrypoint: str, mode: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "hq.platform.core.tests.registries", entrypoint, mode],
        cwd=REPO_ROOT,
        env=os.environ | {"DJANGO_SETTINGS_MODULE": "hq.config.settings", "SEVERINO_DATABASE_PATH": os.devnull},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    line = next(line for line in result.stdout.splitlines() if line.startswith(PREFIX))
    return json.loads(line.removeprefix(PREFIX))


def missing(eager: dict, lazy: dict) -> dict[str, list[str]]:
    """What the eager boot registered and the lazy one did not, by registry.

    A library's receiver is left out when the lazy boot never loaded its
    module: it connects as that module loads, beside the state it serves. HQ's
    own, every check and lookup, and every receiver overriding a setting
    connects, which clears state in other modules, is held whatever loaded.
    """

    loaded, ours = set(lazy["loaded"]), tuple(lazy["ours"])
    found: dict[str, list[str]] = {}
    for registry, names in eager.items():
        if registry in ("loaded", "ours"):
            continue
        if registry == "lookups":
            gone = [
                f"{cls} {name}"
                for cls, registered in names.items()
                if cls in lazy["lookups"]
                for name in sorted(set(registered) - set(lazy["lookups"][cls]))
            ]
        else:
            gone = sorted(set(names) - set(lazy[registry]))
        if registry == "receivers":
            gone = [name for name in gone if _held(module_of(name), loaded, ours)]
        if gone:
            found[registry] = gone
    return found


def _held(module: str, loaded: set[str], ours: tuple[str, ...]) -> bool:
    return module in loaded or module.partition(".")[0] in ours


class LazyBootTests(SimpleTestCase):
    def test_a_lazy_boot_registers_everything_an_eager_one_does(self):
        for entrypoint in ENTRYPOINTS:
            with self.subTest(entrypoint):
                self.assertEqual(missing(booted(entrypoint, "eager"), booted(entrypoint, "lazy")), {}, MOVE_IT)

    def test_what_a_lazy_boot_leaves_out_is_found(self):
        eager = {
            "loaded": [],
            "ours": ["hq"],
            "checks": ["example.checks:check_a"],
            "lookups": {"example.fields:DateField": ["exact", "year"], "example.fields:Unloaded": ["year"]},
            "receivers": [
                "example.cache:close_caches from any sender",
                "example.loaded:changed from any sender",
                "hq.example:forget from any sender",
            ],
            "overrides": ["example.unloaded:storage_changed from any sender"],
        }
        lazy = {
            "loaded": ["example.loaded"],
            "ours": ["hq"],
            "checks": [],
            "lookups": {"example.fields:DateField": ["exact"]},
            "receivers": [],
            "overrides": [],
        }
        self.assertEqual(
            missing(eager, lazy),
            {
                "checks": ["example.checks:check_a"],
                "lookups": ["example.fields:DateField year"],
                "receivers": ["example.loaded:changed from any sender", "hq.example:forget from any sender"],
                "overrides": ["example.unloaded:storage_changed from any sender"],
            },
        )

    def test_reading_what_a_boot_registered_builds_no_lazy_object(self):
        """A lazy object built by the read would connect its receivers partway through it, so
        the read would depend on the order the collector keeps objects in."""

        planted = SimpleLazyObject(object)

        registries()

        self.assertIs(planted._wrapped, empty, "the read built a lazy object it passed")


EAGER_PROBE = """
import importlib, json, sys
from hq.config import imports
imports.EAGER = ("example_eager.entry",)
imports.lazily()
importlib.import_module("example_eager.entry")
importlib.import_module("example_plain")
print(json.dumps(sorted(name for name in sys.modules if name.startswith("example_"))))
"""
PLANTED = {
    "example_eager/__init__.py": "",
    "example_eager/entry.py": "import example_eager.sibling\nimport example_pulled\n",
    "example_eager/sibling.py": "import example_eager.unused\n",
    "example_eager/unused.py": "",
    "example_pulled/__init__.py": "import example_leaf\n",
    "example_leaf.py": "",
    "example_plain.py": "import example_unused\n",
    "example_unused.py": "",
}


class EagerLibraryTests(SimpleTestCase):
    def test_what_an_eager_module_pulls_in_loads_with_it(self):
        """Another library an EAGER module imports loads whole, so it needs no entry of its own.

        The rest of the entry's own library, and everything outside it, stays lazy.
        """

        with tempfile.TemporaryDirectory() as root:
            for name, source in PLANTED.items():
                (Path(root) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(root) / name).write_text(source, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-c", EAGER_PROBE],
                cwd=REPO_ROOT,
                env=os.environ | {"PYTHONPATH": os.pathsep.join((root, str(REPO_ROOT)))},
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                "example_eager",
                "example_eager.entry",
                "example_eager.sibling",
                "example_leaf",
                "example_plain",
                "example_pulled",
            ],
        )


def shadowed(source: str, submodules: set[str]) -> list[str]:
    """Each module-level name a package's ``__init__`` binds that one of its ``submodules`` has.

    Loading ``package.name`` sets the package's attribute ``name`` to that
    submodule, and under a lazy import that happens after the binding, so the
    function or class bound there becomes the module.
    """

    tree = ast.parse(source)
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            # `from . import name` binds the submodule itself.
            binds_submodule = node.level == 1 and node.module is None
            found += [
                f"{node.lineno} {alias.asname or alias.name}"
                for alias in node.names
                if (alias.asname or alias.name) in submodules and not (binds_submodule and alias.asname is None)
            ]
        elif isinstance(node, ast.FunctionDef | ast.ClassDef) and node.name in submodules:
            found.append(f"{node.lineno} {node.name}")
        elif isinstance(node, ast.Assign):
            found += [
                f"{node.lineno} {target.id}"
                for target in node.targets
                if isinstance(target, ast.Name) and target.id in submodules
            ]
    return found


def submodules_of(package: Path) -> set[str]:
    return {path.stem for path in package.glob("*.py") if path.name != "__init__.py"} | {
        path.parent.name for path in package.glob("*/__init__.py")
    }


@tag("source", "tree")
class SubmoduleShadowTests(SimpleTestCase):
    def test_no_package_binds_a_name_its_submodule_has(self):
        found = [
            f"{init.relative_to(REPO_ROOT)}:{hit}"
            for root in PYTHON_ROOTS
            for init in sorted((REPO_ROOT / root).rglob("__init__.py"))
            for hit in shadowed(init.read_text(encoding="utf-8"), submodules_of(init.parent))
        ]
        self.assertEqual(found, [], "rename the submodule, so the name keeps what the package binds to it")

    def test_a_planted_shadow_is_found(self):
        planted = (
            "from . import tones\n"
            "from .release import Release, release\n"
            "from .sight import sight as seen\n"
            "from .work import work_json\n"
            "def tones(): ...\n"
        )
        self.assertEqual(shadowed(planted, {"release", "sight", "tones", "work"}), ["2 release", "5 tones"])
