"""A process that judges what HQ reaches by what it loaded imports as written (hq.config.imports.eagerly).

Under lazy imports a module-level import loads only once its name is used, so
a process that reads ``sys.modules`` after importing HQ lazily calls a library
every real use reaches unused.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.test_settings_env import REPO_ROOT

PROBE = """
import importlib, json, sys
from hq.config import imports
{before}
imports.lazily()
importlib.import_module("example_entry")
print(json.dumps(sorted(name for name in sys.modules if name.startswith("example_"))))
"""
PLANTED = {
    "example_entry.py": "import example_named\nlazy import example_waiting\n",
    "example_named.py": "",
    "example_waiting.py": "",
}
# The image checks that read what HQ loaded, each a probe run in the built image.
IMAGE_CHECKS = REPO_ROOT / "mise-tasks" / "image" / "verify"


def run(before: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as root:
        for name, source in PLANTED.items():
            (Path(root) / name).write_text(source, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-c", PROBE.format(before=before)],
            cwd=REPO_ROOT,
            env=os.environ | {"PYTHONPATH": os.pathsep.join((root, str(REPO_ROOT)))},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


class EagerlyTests(SimpleTestCase):
    def test_an_entrypoint_switching_lazy_imports_on_leaves_them_as_written(self):
        result = run("imports.eagerly()")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["example_entry", "example_named"])

    def test_a_lazy_process_holds_only_what_its_run_used(self):
        result = run("")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["example_entry"])

    def test_called_once_imports_are_lazy_it_refuses(self):
        result = run("imports.lazily(); imports.eagerly()")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("call eagerly() first", result.stderr)


@tag("source")
class ImageCheckTests(SimpleTestCase):
    def test_an_image_check_reading_what_hq_loaded_imports_eagerly_first(self):
        """Its probe calls ``eagerly()`` before it imports Django or HQ, which may switch lazy imports on."""

        readers = [path for path in sorted(IMAGE_CHECKS.iterdir()) if "sys.modules" in path.read_text(encoding="utf-8")]
        self.assertNotEqual(readers, [], f"no image check under {IMAGE_CHECKS} reads sys.modules")
        for path in readers:
            with self.subTest(path.name):
                self.assertEqual(lazy_before_eagerly(path.read_text(encoding="utf-8")), [])

    def test_a_probe_importing_django_first_or_never_switching_is_found(self):
        late = "import sys\nimport django\nfrom hq.config.imports import eagerly\n\neagerly()\nsys.modules\n"
        self.assertEqual(lazy_before_eagerly(late), ["import django"])
        self.assertEqual(lazy_before_eagerly("import django\nsys.modules\n"), ["eagerly() is never called"])


def lazy_before_eagerly(source: str) -> list[str]:
    """Each import of Django or HQ a probe's ``source`` runs before ``eagerly()``, or the call, missing."""

    call = re.search(r"^eagerly\(\)$", source, re.MULTILINE)
    if call is None:
        return ["eagerly() is never called"]
    found = re.findall(r"^(?:import|from) (?:django|hq)\b.*$", source[: call.start()], re.MULTILINE)
    return [line for line in found if line != "from hq.config.imports import eagerly"]
