"""A patch over a name another module imported lazily leaves both as they were."""

import importlib
import sys
import tempfile
import uuid
from pathlib import Path
from types import LazyImportType
from unittest import mock

from django.test import SimpleTestCase


class LazyPatchTests(SimpleTestCase):
    def modules(self, *, load=True, imports="from .source import read", reads="read"):
        """A fresh pair: ``source`` defines ``read``, and ``user`` imports it from there as ``imports`` says."""

        folder = Path(tempfile.mkdtemp())
        package = f"example_lazy_{uuid.uuid4().hex}"
        (folder / package).mkdir()
        (folder / package / "__init__.py").write_text("")
        (folder / package / "source.py").write_text('def read():\n    return "real"\n')
        (folder / package / "user.py").write_text(f"{imports}\n\n\ndef ask():\n    return {reads}()\n")
        sys.path.insert(0, str(folder))
        self.addCleanup(sys.path.remove, str(folder))
        self.addCleanup(lambda: [sys.modules.pop(name) for name in list(sys.modules) if name.startswith(package)])
        if not load:
            return package, None
        user = importlib.import_module(f"{package}.user")
        self.assertIsInstance(user.__dict__[reads], LazyImportType, "the suite imports lazily, as HQ does")
        return package, user

    def test_a_module_first_loaded_while_its_source_is_patched_reads_the_real_one_after(self):
        package, _ = self.modules(load=False)
        importlib.import_module(f"{package}.source")
        with mock.patch(f"{package}.source.read", return_value="patched"):
            user = importlib.import_module(f"{package}.user")
            user.ask()
        self.assertEqual(user.ask(), "real")

    def test_a_name_first_read_while_its_source_is_patched_is_the_real_one_after(self):
        package, user = self.modules()
        with mock.patch(f"{package}.source.read", return_value="patched"):
            user.ask()
        self.assertEqual(user.ask(), "real")

    def test_a_name_imported_under_another_while_its_source_is_patched_is_the_real_one_after(self):
        for imports in (
            "from .source import read as fetch",
            "from .source import (\n    read as fetch,\n)",
            "from . import source\nfrom .source import read as fetch, read",
        ):
            with self.subTest(imports=imports):
                package, user = self.modules(imports=imports, reads="fetch")
                with mock.patch(f"{package}.source.read", return_value="patched"):
                    self.assertEqual(user.ask(), "patched")
                self.assertEqual(user.ask(), "real")

    def test_patching_the_source_and_then_the_user_restores_both(self):
        package, user = self.modules()
        with (
            mock.patch(f"{package}.source.read", return_value="source patched"),
            mock.patch(f"{package}.user.read", return_value="user patched"),
        ):
            self.assertEqual(user.ask(), "user patched")
        self.assertEqual(user.ask(), "real")

    def test_a_patch_over_a_lazy_name_whose_module_is_missing_imports_nothing(self):
        package, _ = self.modules(load=False)
        source = importlib.import_module(f"{package}.source")
        (Path(source.__file__).parent / "absent_user.py").write_text("from .not_installed import thing\n")
        absent = importlib.import_module(f"{package}.absent_user")
        with mock.patch.object(absent, "thing", "patched"):
            pass
        self.assertIsInstance(absent.__dict__["thing"], LazyImportType, "the name is put back unimported")
