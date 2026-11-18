"""What fast judges when its fixes rewrite a file, the tree they leave named as uncommitted, and which
gates its selection can pick."""

import contextlib
import io
import os
import subprocess
import tempfile
from pathlib import Path, PurePath
from unittest import mock

from django.test import SimpleTestCase, tag

from tests.mise_tasks import mise_tasks, runs
from tools.gate import chain, fast, verdict
from tools.gate.tests.base import fixture_git
from tools.gate.verdict import TREE_LINE, Gate, Step


class FixedFirstTests(SimpleTestCase):
    def test_a_rewritten_derived_file_is_judged_as_rewritten_and_named_in_the_closing_line(self):
        derived = "example/derived.json"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder, "checkout")
            (root / "example").mkdir(parents=True)
            git = fixture_git(root)
            git("init", "--quiet")
            (root / derived).write_text('{"example": 1}\n')
            (root / "example/source.py").write_text("EXAMPLE = 1\n")
            git("add", "--all")
            git("commit", "--quiet", "-m", "base")
            base = git("rev-parse", "HEAD")
            (root / "example/source.py").write_text("EXAMPLE = 2\n")

            real = subprocess.run

            def writer(command, *args, **kwargs):
                """The fix mode's run of mise writes the derived file; every other command runs."""

                if command[0] != "mise":
                    return real(command, *args, **kwargs)
                (root / derived).write_text('{"example": 2}\n')
                return subprocess.CompletedProcess(command, 0)

            screen = io.StringIO()
            # As a run here: one sent to another machine writes no fix.
            with (
                mock.patch.dict("os.environ", {chain.SENT: ""}),
                mock.patch.object(chain, "ROOT", root),
                mock.patch.object(verdict, "VERDICTS", Path(folder, "verdicts")),
                mock.patch.object(fast, "quick_tasks", return_value={}),
                mock.patch.object(fast, "fixers", return_value=("example:drift",)),
                mock.patch.object(fast.subprocess, "run", side_effect=writer),
                contextlib.redirect_stdout(screen),
                mock.patch.object(fast, "record_pass") as recorded,
            ):
                gate = Gate("fast", root)
                paths, fixed = fast.fixed_first(gate, ("example:drift",), base, ([], []))
                status = fast.judge(gate, [Step("example")], base, whole=False, fixed=fixed)

            self.assertEqual(status, 0)
            self.assertIn(derived, paths[0])
            # A commit of what the fixes rewrote, beside the change, is the tree the verdict names.
            git("add", "--all")
            committed = git("write-tree")
            kept = Path(folder, "verdicts", "fast.txt").read_text().splitlines()
            self.assertEqual(kept[0], TREE_LINE.format(committed))
            self.assertEqual(recorded.call_args.args[0], committed)
            closing = screen.getvalue().splitlines()[-1]
            self.assertIn(f"{fast.REWROTE} {derived}", closing)


@tag("source")
class SelectableTests(SimpleTestCase):
    def test_every_gate_fast_may_run_is_selected_by_what_it_reads(self):
        """fast runs a gate only when mise's --affected picks it among those tagged QUICK, by its
        sources: a gate tagged HEAVY alone is never picked, and one with no sources is picked on
        every change."""

        from tests.mise_tasks import matched

        listed = mise_tasks()
        heavy = [name for name, task in listed.items() if fast.HEAVY in (task.get("tags") or ())]
        self.assertTrue(heavy, f"no task is tagged {fast.HEAVY}, so this holds nothing")
        for name in heavy:
            with self.subTest(name):
                self.assertIn(
                    fast.QUICK,
                    listed[name]["tags"],
                    f"{name} is tagged {fast.HEAVY}, which only fast reads, but not {fast.QUICK}, so fast"
                    f" never selects it: tag it {fast.QUICK} and declare what it reads as its sources",
                )
        for name, task in listed.items():
            if fast.QUICK not in (task.get("tags") or ()):
                continue
            with self.subTest(name):
                self.assertTrue(
                    task["sources"],
                    f"{name} declares no sources, so mise's --affected selects it on every change:"
                    " declare what it reads",
                )
                missed = [
                    relative
                    for relative in (os.path.relpath(path, task["dir"]) for path in runs(task))
                    if not matched(PurePath(relative), task["sources"])
                ]
                self.assertFalse(missed, f"{name} runs {', '.join(missed)}, which its sources leave out: add each")
