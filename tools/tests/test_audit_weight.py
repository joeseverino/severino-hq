"""audit:dependency-weight weighs each locked distribution by its RECORD, the runtime set apart."""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from tools import audit_weight

# A RECORD's rows as PEP 376 writes them: a path holding a comma quoted, and RECORD's own row
# without a size.
_RECORD = (
    'example_left/__init__.py,sha256=a,1024\n"example_left/a,b.py",sha256=b,1024\nexample_left-1.dist-info/RECORD,,\n'
)


class InstalledTests(SimpleTestCase):
    def test_a_distribution_weighs_what_its_record_lists_under_its_normalized_name(self) -> None:
        with tempfile.TemporaryDirectory() as site:
            info = Path(site) / "Example_Left-1.dist-info"
            info.mkdir()
            (info / "METADATA").write_text("Metadata-Version: 2.4\nName: Example_Left\nVersion: 1\n", encoding="utf-8")
            (info / "RECORD").write_text(_RECORD, encoding="utf-8")
            # importlib.metadata lists only the files RECORD names that are there.
            (Path(site) / "example_left").mkdir()
            for name in ("__init__.py", "a,b.py"):
                (Path(site) / "example_left" / name).write_bytes(b"")
            self.assertEqual(audit_weight.installed([site]), {"example-left": 2048})


class ReportTests(SimpleTestCase):
    def setUp(self) -> None:
        every = {"example-left": "1", "example-right": "2", "example-absent": "3"}
        sizes = {"example-left": 2048, "example-right": 4096, "example-stray": 512}
        self.lines = audit_weight.report(every, ["example-left"], sizes)

    def row(self, name: str) -> str:
        return next(line for line in self.lines if line.endswith(f" {name}"))

    def test_each_locked_distribution_is_runtime_or_dev_largest_first(self) -> None:
        self.assertIn("runtime", self.row("example-left 1"))
        self.assertIn("dev", self.row("example-right 2"))
        self.assertLess(self.lines.index(self.row("example-right 2")), self.lines.index(self.row("example-left 1")))

    def test_one_the_environment_lacks_is_said_and_a_stray_is_counted_apart(self) -> None:
        self.assertIn("not installed here", self.row("example-absent 3"))
        self.assertIn("1 installed but not locked", self.lines[-1])
        self.assertIn("1 runtime distributions", self.lines[-1])
