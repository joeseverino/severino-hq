"""An extension imports the host only through hq_sdk, and keeps no cache of its own.

check-plugin.sh holds a package's source to tools/pipeline/extension-imports.toml
with Ruff; these run that config over planted sources.
"""

import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "tools" / "pipeline" / "extension-imports.toml"


def refused(files: dict[str, str]) -> list[str]:
    """Each import the config refuses in ``files``, as ``path:line: module``,
    and each file that does not parse, as ``path:line: invalid syntax``."""

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        ran = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", "--config", str(CONFIG), "--output-format", "json"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    found = []
    for problem in json.loads(ran.stdout):
        where = f"{Path(problem['filename']).relative_to(root.resolve()).as_posix()}:{problem['location']['row']}"
        if problem["code"] == "invalid-syntax":
            found.append(f"{where}: invalid syntax")
        else:
            found.append(f"{where}: {problem['message'].split('`')[1]}")
    return sorted(set(found))


@tag("source")
class ExtensionImportTests(SimpleTestCase):
    def test_the_sdk_is_the_one_host_package_an_extension_imports(self):
        self.assertEqual(
            refused({"valid.py": "from hq_sdk.capabilities import CapabilitySpec\nimport hq_sdk.ui\n"}), []
        )

    def test_every_form_of_a_host_import_is_refused(self):
        source = (
            "from hq.platform import application\n"
            "from hq import platform\n"
            "import hq.domains.projects\n"
            "import os, hq.jobs.runner\n"
            "lazy from hq.platform.application.asks import Ask\n"
            "def f():\n"
            "    from hq import audit; from hq import audit\n"
            "from hq_sdk import ui\n"
        )
        self.assertEqual(
            refused({"pkg/plugin.py": source}),
            [f"pkg/plugin.py:{line}: hq" for line in (1, 2, 3, 4, 5, 7)],
        )

    def test_a_package_named_like_a_host_app_is_its_own(self):
        source = "from core import audit\nimport jobs.runner\nfrom calendars.models import Entry\nimport hqueue\n"
        self.assertEqual(refused({"plugin.py": source}), [])

    def test_a_relative_import_is_the_extensions_own_whatever_it_is_called(self):
        source = "from .calendars import sources\nfrom . import hq\nfrom hq.domains.calendars import Entry\n"
        self.assertEqual(refused({"pkg/__init__.py": "", "pkg/plugin.py": source}), ["pkg/plugin.py:3: hq"])

    def test_a_cache_of_its_own_is_refused(self):
        source = (
            "from django.core.cache import cache\nfrom django.core import cache as c\nfrom django.core import signing\n"
        )
        self.assertEqual(
            refused({"projection.py": source}),
            ["projection.py:1: django.core.cache", "projection.py:2: django.core.cache"],
        )

    def test_a_file_that_does_not_parse_is_refused(self):
        self.assertIn("broken.py:1: invalid syntax", refused({"broken.py": "def (:\n"}))

    def test_the_banned_packages_are_the_packages_of_the_host_tree(self):
        banned = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["lint"]["flake8-tidy-imports"]["banned-api"]
        packages = {
            entry.name
            for entry in ROOT.iterdir()
            if entry.is_dir() and (entry / "__init__.py").exists() and entry.name != "hq_sdk"
        }
        self.assertEqual({name for name in banned if "." not in name}, packages)
