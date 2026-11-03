"""No module of HQ's derives a value from the plugin composition while it is being imported.

HQ's imports are lazy, so a module body runs wherever the module is first
used: in a request before the composition is ready, or in a test worker inside
a block that installs another set, which then holds for the rest of the
process. A value derived from the composition is derived at its call.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.composition_reads import PREFIX
from hq.platform.core.tests.test_settings_env import REPO_ROOT

OURS = ("hq", "hq_sdk")

# Read once per process by Django itself, which a test changes only through the setting.
ONCE_PER_PROCESS = {
    # The app registry is built from INSTALLED_APPS; a test that installs an
    # extension's apps does so through modify_settings(INSTALLED_APPS).
    "hq.config.settings",
    # The URLconf is resolved once per ROOT_URLCONF; a test that mounts an
    # extension's routes overrides ROOT_URLCONF, which clears the resolver.
    "hq.config.urls",
}

MOVE_IT = "derive it at its call (a function, cached through the composition's own caches), never as the module loads"


def module_names(root: Path, packages: tuple[str, ...]) -> list[str]:
    """Every module under ``packages`` outside a tests package, by dotted name."""

    names = []
    for package in packages:
        for path in sorted((root / package).rglob("*.py")):
            parts = path.relative_to(root).with_suffix("").parts
            if "tests" in parts:
                continue
            names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def composition_reads(modules: list[str], pythonpath: str = "") -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "hq.platform.core.tests.composition_reads", *modules],
        cwd=REPO_ROOT,
        env=os.environ
        | {
            "DJANGO_SETTINGS_MODULE": "hq.config.settings",
            "SEVERINO_DATABASE_PATH": os.devnull,
            "PYTHONPATH": os.pathsep.join(part for part in (pythonpath, str(REPO_ROOT)) if part),
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    line = next(line for line in result.stdout.splitlines() if line.startswith(PREFIX))
    return json.loads(line.removeprefix(PREFIX))


def module_of(read: str) -> str:
    return read.partition(":")[0]


PLANTED = {
    "example_reads/__init__.py": "",
    # Through a helper of its own, under a name no scan would look for.
    "example_reads/helper.py": (
        "from hq.platform.application.plugins import installed_plugins\n"
        "def ids():\n"
        "    return tuple(plugin.id for plugin in installed_plugins())\n"
    ),
    "example_reads/frozen.py": "from example_reads.helper import ids\nFROZEN = ids()\n",
    # Two modules reading one cached graph, the second after importing the first: both are named.
    "example_reads/graph_a.py": (
        "from hq.platform.application.integrations import integration_graph\nKINDS = integration_graph().resources\n"
    ),
    "example_reads/graph_b.py": (
        "from hq.platform.application.integrations import integration_graph\n"
        "from example_reads.graph_a import KINDS\n"
        "CAPABILITIES = sorted(integration_graph().capabilities)\n"
    ),
    # Read only at the call.
    "example_reads/deferred.py": "from example_reads.helper import ids\ndef current():\n    return ids()\n",
}


@tag("source", "tree")
class CompositionAtImportTests(SimpleTestCase):
    def test_no_module_reads_the_composition_as_it_loads(self):
        found = composition_reads(module_names(REPO_ROOT, OURS))
        self.assertEqual([read for read in found if module_of(read) not in ONCE_PER_PROCESS], [], MOVE_IT)
        self.assertEqual(
            ONCE_PER_PROCESS - {module_of(read) for read in found},
            set(),
            "this module no longer reads the composition as it loads: drop it from ONCE_PER_PROCESS",
        )

    def test_a_planted_read_is_named_through_any_call_chain(self):
        with tempfile.TemporaryDirectory() as root:
            for name, source in PLANTED.items():
                (Path(root) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(root) / name).write_text(source, encoding="utf-8")
            found = composition_reads(module_names(Path(root), ("example_reads",)), pythonpath=root)
        self.assertEqual(
            [read for read in found if module_of(read).startswith("example_reads")],
            [
                "example_reads.frozen:2 FROZEN = ids()",
                "example_reads.graph_a:2 KINDS = integration_graph().resources",
                "example_reads.graph_b:3 CAPABILITIES = sorted(integration_graph().capabilities)",
            ],
        )
