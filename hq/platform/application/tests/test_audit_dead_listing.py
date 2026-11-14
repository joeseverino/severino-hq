"""audit:dead counts a test's read as a use only of a name a test-support module defines."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from tests.mise_tasks import ROOT

SCRIPT = ROOT / "mise-tasks" / "audit" / "dead"
SUPPORT = "def tested_seam():\n    pass\n\n\ndef unread_seam():\n    pass\n"
PRODUCT = "def tested_only():\n    pass\n"
TEST = "from hq import a_testing, product\n\na_testing.tested_seam()\nproduct.tested_only()\n"


@tag("source")
class AuditDeadListingTests(SimpleTestCase):
    def test_a_tests_read_keeps_a_seam_alive_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "hq" / "tests").mkdir(parents=True)
            for empty in ("hq_sdk", "tools", "tests", "demo", "examples", "templates"):
                (root / empty).mkdir()
            (root / "tools" / "lint").mkdir()
            (root / "tools" / "lint" / "vulture-whitelist.py").touch()
            (root / "manage.py").touch()
            shutil.copyfile(ROOT / "pyproject.toml", root / "pyproject.toml")
            (root / "hq" / "a_testing.py").write_text(SUPPORT)
            (root / "hq" / "product.py").write_text(PRODUCT)
            (root / "hq" / "tests" / "test_a.py").write_text(TEST)
            # ripgrep is lazy, so mise installs it on its first use and puts it
            # on PATH only for a command it runs, as `mise run audit:dead` does.
            in_folder = ["/bin/sh", "-c", 'cd "$1" && exec /bin/sh "$2"', "sh", str(root), str(SCRIPT)]
            run = subprocess.run(
                ["mise", "-C", str(ROOT), "exec", "ripgrep", "--", *in_folder],
                env=os.environ | {"UV_PROJECT": str(ROOT), "HQ_LOCAL_PYTHONPATH": ""},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(run.returncode, 0, run.stderr)
        names = {
            line.split()[1]
            for line in run.stdout.splitlines()
            if line.startswith(("hq/a_testing.py:", "hq/product.py:"))
        }
        self.assertEqual(names, {"unread_seam", "tested_only"})
