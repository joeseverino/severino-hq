"""The image-tree fixture copies the libraries its task sources as the task's own lines name them, so a library the task begins to source needs no list kept beside it.

A module of its own, so ``test --at`` can run it beside the fixture as a ref has it (``--keep``)."""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, tag

from hq.platform.application.tests import test_image_tree

ROOT = test_image_tree.ROOT
PLANTED = ". ./tools/lib/example.sh\n"


@tag("source")
class ImageTreeLibrariesTests(SimpleTestCase):
    def test_a_library_the_task_begins_to_source_is_copied(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(ROOT / "tools/lib", root / "tools/lib")
            (root / "tools/lib/example.sh").write_text(":\n")
            for name in ("Dockerfile", "Dockerfile.dockerignore"):
                shutil.copy(ROOT / name, root)
            lines = (ROOT / test_image_tree.CHECK).read_text().splitlines(keepends=True)
            first = next(index for index, line in enumerate(lines) if line.startswith(". ./tools/lib/"))
            (root / test_image_tree.CHECK).parent.mkdir(parents=True)
            (root / test_image_tree.CHECK).write_text("".join([*lines[:first], PLANTED, *lines[first:]]))
            with mock.patch.object(test_image_tree, "ROOT", root):
                result = test_image_tree.ImageTreeTests().check(test_image_tree.SHIPPED)
        self.assertEqual(result.returncode, 0, result.stderr)
