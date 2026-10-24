"""The structural bar runs the graph release mise.toml pins, and holds only
when every pair outside tests is in the baseline of a graph that indexed something."""

import io
import json
import tempfile
import tomllib
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, tag

from tools.structural import bar

PACKAGE = f"github:DeusData/{bar.TOOL}"
COLUMNS = ["a.file_path", "b.file_path", "a.name", "b.name"]
# Two functions alike enough for the graph, outside tests, and one pair inside them.
SOURCE = {
    "pkg/a.py": "def alpha(items):\n    return sorted(items)\n",
    "pkg/b.py": "def beta(rows):\n    return sorted(rows)\n",
}
PAIR = "similar pkg/a.py::alpha <> pkg/b.py::beta"
OUTSIDE = ["pkg/a.py", "pkg/b.py", "alpha", "beta"]
INSIDE = ["pkg/tests/test_a.py", "pkg/tests/test_b.py", "test_alpha", "test_beta"]


def answer(rows: list[list[str]]) -> list[dict]:
    """One whole page of the graph's answer, as query_graph returns it."""

    page = {"columns": COLUMNS, "rows": rows, "total": len(rows), "total_relation": "eq", "has_more": False}
    return [{"content": [{"text": json.dumps(page)}]}]


@tag("source")
class GraphToolTests(SimpleTestCase):
    def test_the_command_the_bar_runs_installs_the_release_mise_pins(self):
        """A lazy tool from a github: backend installs on first use only of
        the commands its lazy_bins names."""
        declared = tomllib.loads((bar.ROOT / "mise.toml").read_text(encoding="utf-8"))["tools"]
        self.assertIn(bar.TOOL, declared[PACKAGE].get("lazy_bins", []))


class CompareTests(SimpleTestCase):
    def bar(self, rows: list[list[str]], baseline: list[str]) -> tuple[int, str]:
        """The bar's verdict on a graph answering with these rows, against this baseline."""

        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            for path, text in SOURCE.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            (root / "baseline.txt").write_text(bar.HEADER + "".join(f"{line}\n" for line in baseline))
            for name, value in {
                "ROOT": root,
                "BASELINE": root / "baseline.txt",
                "cli": lambda *_: {},
                "build_inspector": lambda store: store / "inspector",
                "graph_pages": lambda *_: answer(rows),
            }.items():
                stack.enter_context(patch.object(bar, name, value))
            err = stack.enter_context(patch("sys.stderr", new_callable=io.StringIO))
            stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
            return bar.main([]), err.getvalue()

    def test_a_pair_the_baseline_does_not_list_fails_and_is_named(self):
        code, err = self.bar([OUTSIDE, INSIDE], baseline=[])

        self.assertEqual(code, 1)
        self.assertIn("pkg/a.py::alpha <> pkg/b.py::beta", err)

    def test_a_listed_pair_holds_and_a_pair_inside_tests_is_never_counted(self):
        self.assertEqual(self.bar([OUTSIDE, INSIDE], baseline=[PAIR])[0], 0)
        self.assertEqual(self.bar([INSIDE], baseline=[])[0], 0)

    def test_a_listed_pair_that_left_still_holds(self):
        self.assertEqual(self.bar([INSIDE], baseline=[PAIR])[0], 0)

    def test_a_graph_with_no_pairs_at_all_fails(self):
        """Tests alone hold similar pairs, so an answer with none means nothing was indexed."""
        code, err = self.bar([], baseline=[])

        self.assertEqual(code, 1)
        self.assertIn("index is empty", err)
