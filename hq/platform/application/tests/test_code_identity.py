"""The identity of the running code: shared by every copy of it, moved by any change."""

import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from hq.platform.application import code_identity
from hq.platform.application.plugins import PLUGIN_API_VERSION, PluginManifest

PACKAGE = "example_identity"
FIRST = "from dataclasses import dataclass\n\n@dataclass\nclass Node:\n    key: str\n"
SECOND = FIRST + "    facts: tuple = ()\n"


@contextmanager
def importable(files: dict[str, str]):
    """A top-level package of ``files`` on the import path, as a process would find it."""

    with TemporaryDirectory() as directory:
        root = Path(directory, PACKAGE)
        for name, text in {"__init__.py": "", **files}.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        with mock.patch.object(sys, "path", [directory, *sys.path]):
            yield root


def identity() -> str:
    return code_identity.code_identity([PACKAGE])


class CodeIdentityTests(SimpleTestCase):
    def test_an_installed_extension_is_part_of_the_running_code(self):
        manifest = PluginManifest(
            id="example.notes",
            name="Example Notes",
            version="1.2.3",
            distribution="example-notes",
            source_repository="example/example-notes",
            source_workflow=".github/workflows/admit-plugin.yml",
            api_version=PLUGIN_API_VERSION,
            integration_provider=f"{PACKAGE}.plugin:integration",
        )
        with (
            importable({"plugin.py": FIRST}) as root,
            mock.patch.object(code_identity, "installed_plugins", return_value=(manifest,)),
        ):
            before = code_identity.running_code()
            (root / "plugin.py").write_text(SECOND)
            self.assertNotEqual(code_identity.running_code(), before)

    def test_a_package_with_no_source_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            code_identity.code_identity(["example_absent"])
