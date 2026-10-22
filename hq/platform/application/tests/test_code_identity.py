"""The identity of the running code: shared by every copy of it, moved by any change."""

import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.conf import settings
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
    def test_two_copies_of_the_same_code_agree(self):
        with importable({"nodes.py": FIRST}):
            here = identity()
            self.assertEqual(identity(), here)
        with importable({"nodes.py": FIRST}):
            self.assertEqual(identity(), here)

    def test_a_class_that_changes_shape_moves_it(self):
        with importable({"nodes.py": FIRST}) as root:
            before = identity()
            (root / "nodes.py").write_text(SECOND)
            self.assertNotEqual(identity(), before)

    def test_a_module_added_or_moved_moves_it(self):
        with importable({"nodes.py": FIRST}) as root:
            before = identity()
            (root / "more.py").write_text("")
            added = identity()
            (root / "more.py").rename(root / "other.py")
            self.assertEqual(len({before, added, identity()}), 3)

    def test_an_upgraded_distribution_moves_it(self):
        with importable({"nodes.py": FIRST}):
            before = identity()
            with mock.patch.object(code_identity, "_distributions", return_value=["example-0.2.dist-info"]):
                self.assertNotEqual(identity(), before)

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

    def test_the_working_directory_on_the_path_is_read_as_one(self):
        """``python -c`` puts an empty entry first on the import path."""

        with importable({"nodes.py": FIRST}):
            before = identity()
            with mock.patch.object(sys, "path", ["", *sys.path]):
                self.assertEqual(identity(), before)

    def test_a_package_with_no_source_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            code_identity.code_identity(["example_absent"])

    def test_the_derived_cache_is_keyed_on_the_running_code(self):
        self.assertEqual(settings.CACHES["derived"]["KEY_PREFIX"], code_identity.running_code())
