"""The structural bar runs the graph release mise.toml pins, and holds only
when every pair outside tests is in the baseline of a graph that indexed something."""

import io
import json
import subprocess
import tempfile
import tomllib
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.task import Usage
from tools.structural import bar, classify

PACKAGE = f"github:DeusData/{bar.TOOL}"
# Two functions alike enough for the graph, outside tests, and one pair inside them.
SOURCE = {
    "pkg/a.py": "def alpha(items):\n    return sorted(items)\n",
    "pkg/b.py": "def beta(rows):\n    return sorted(rows)\n",
}
PAIR = "similar pkg/a.py::alpha <> pkg/b.py::beta"
FP = "0" * 511
OUTSIDE = [["pkg/a.py", "alpha", "", FP], ["pkg/b.py", "beta", "", FP]]
INSIDE = [["pkg/tests/test_a.py", "test_alpha", "", FP], ["pkg/tests/test_b.py", "test_beta", "", FP]]


def answer(rows: list[list[str]]) -> list[dict]:
    """One whole page of the graph's answer, as query_graph returns it."""

    page = {"columns": classify.COLUMNS, "rows": rows, "total": len(rows), "total_relation": "eq", "has_more": False}
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
            keys = iter(range(1_000_000))
            for name, value in {
                "ROOT": root,
                "BASELINE": root / "baseline.txt",
                "CACHE": root / "cache",
                # Inputs never seen before, so the graph always answers.
                "inputs_key": lambda: str(next(keys)),
                "cli": lambda *_: {},
                "build_inspector": lambda store: store / "inspector",
                "graph_pages": lambda *_: answer(rows),
            }.items():
                stack.enter_context(patch.object(bar, name, value))
            err = stack.enter_context(patch("sys.stderr", new_callable=io.StringIO))
            stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
            return bar.main(Usage({})), err.getvalue()

    def test_a_pair_the_baseline_does_not_list_fails_and_is_named(self):
        code, err = self.bar(OUTSIDE + INSIDE, baseline=[])

        self.assertEqual(code, 1)
        self.assertIn("pkg/a.py::alpha <> pkg/b.py::beta", err)
        # The last line counts the pairs and names the cure and the command that lists them.
        closing = err.splitlines()[-1]
        self.assertRegex(closing, r"^1 new near-duplicate pair\b")
        self.assertIn("merge", closing)
        self.assertIn("`mise run lint:structural`", closing)

    def test_a_listed_pair_holds_and_a_pair_inside_tests_is_never_counted(self):
        self.assertEqual(self.bar(OUTSIDE + INSIDE, baseline=[PAIR])[0], 0)
        self.assertEqual(self.bar(INSIDE, baseline=[])[0], 0)

    def test_a_listed_pair_that_left_still_holds(self):
        self.assertEqual(self.bar(INSIDE, baseline=[PAIR])[0], 0)

    def test_a_graph_with_no_fingerprint_at_all_fails(self):
        """Every tree holds functions large enough to compare, so an answer with none indexed nothing."""
        code, err = self.bar([], baseline=[])

        self.assertEqual(code, 1)
        self.assertIn("index is empty", err)


@tag("source", "tree")
class CacheTests(SimpleTestCase):
    """The bar reads the graph once per set of inputs, and its verdict is the same either way."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        # The checkout ignores var/, where the cache lives.
        for path, text in SOURCE.items() | {".gitignore": "var/\n"}.items():
            self.put(path, text)
        subprocess.run(["git", "add", "--all"], cwd=self.root, check=True)
        self.baseline([])
        self.indexed = 0

    def put(self, path: str, text: str) -> None:
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text)

    def baseline(self, lines: list[str]) -> None:
        (self.root / "baseline.txt").write_text(bar.HEADER + "".join(f"{line}\n" for line in lines))

    def bar(self) -> tuple[int, str]:
        """The bar's verdict on this tree, whose graph answers with the pair outside tests."""

        def index(*_: object) -> dict:
            self.indexed += 1
            return {}

        with ExitStack() as stack:
            for name, value in {
                "ROOT": self.root,
                "BASELINE": self.root / "baseline.txt",
                "CACHE": self.root / "var" / "structural",
                # Any release that answers --version stands in for the graph's.
                "TOOL": "git",
                "cli": index,
                "build_inspector": lambda store: store / "inspector",
                "graph_pages": lambda *_: answer(OUTSIDE),
            }.items():
                stack.enter_context(patch.object(bar, name, value))
            err = stack.enter_context(patch("sys.stderr", new_callable=io.StringIO))
            stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
            return bar.main(Usage({})), err.getvalue()

    def test_unchanged_inputs_answer_from_the_cache_with_the_same_verdict(self):
        first, first_err = self.bar()
        second, second_err = self.bar()

        self.assertEqual(self.indexed, 1)
        self.assertEqual((first, second), (1, 1))
        self.assertIn(PAIR.removeprefix("similar "), first_err)
        self.assertIn(PAIR.removeprefix("similar "), second_err)

    def test_a_changed_input_reads_the_graph_again(self):
        self.bar()
        changes = {
            "an edit": lambda: self.put("pkg/a.py", "def alpha(items):\n    return sorted(items, reverse=True)\n"),
            "a new file": lambda: self.put("pkg/c.py", "def gamma():\n    return 1\n"),
            "that file tracked": lambda: subprocess.run(["git", "add", "pkg/c.py"], cwd=self.root, check=True),
            "a deletion": (self.root / "pkg/b.py").unlink,
        }
        for name, change in changes.items():
            with self.subTest(name):
                read = self.indexed
                change()
                self.bar()
                self.assertEqual(self.indexed, read + 1)

    def test_a_baseline_change_alone_reuses_the_pairs_and_judges_them_against_the_new_baseline(self):
        self.assertEqual(self.bar()[0], 1)
        self.baseline([PAIR])

        self.assertEqual(self.bar()[0], 0)
        self.assertEqual(self.indexed, 1)
