"""audit:ast lists each finding where it is, so two runs are compared by a diff."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from tests.mise_tasks import ROOT

SCRIPT = ROOT / "mise-tasks" / "audit" / "ast"
RULES = ROOT / "tools" / "audit" / "ast-grep.yml"
# A docstring of nine lines from line 2, a short one, and getattr sites on
# lines 15 and 16, the second without a default; b.py sorts after a.py.
A = (
    "def long():\n"
    '    """One.\n' + "".join(f"    {n}\n" for n in range(7)) + '    """\n'
    "\n\n"
    "def short():\n"
    '    """Short."""\n'
    "    getattr(short, 'name', None)\n"
    "    getattr(short, 'name')\n"
)
B = "x = getattr(object, 'y', 0)\n"


@tag("source")
class AuditAstListingTests(SimpleTestCase):
    def test_each_finding_is_listed_at_its_lines_in_order_then_counted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tools" / "audit").mkdir(parents=True)
            shutil.copyfile(RULES, root / "tools" / "audit" / "ast-grep.yml")
            (root / "b.py").write_text(B)
            (root / "a.py").write_text(A)
            run = subprocess.run(
                ["/bin/sh", str(SCRIPT)],
                cwd=root,
                env=os.environ | {"usage_globs": ""},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(run.returncode, 0, run.stderr)
        *listed, closing = run.stdout.splitlines()
        self.assertEqual(
            [line.split("\t")[0] for line in listed],
            ["a.py:2-10", "a.py:15", "b.py:1"],
        )
        self.assertIn("long-docstring", listed[0])
        self.assertIn("getattr-with-default", listed[1])
        self.assertRegex(closing, r"^1 docstrings? over 8 lines, 2 getattr")
