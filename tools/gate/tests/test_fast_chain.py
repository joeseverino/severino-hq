"""fast's chain of passed trees: a pass is recorded only when the run proves
its tree, and a base with no pass here starts the chain from CI's run on it."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.test import tag

from tools.gate import chain as fast_chain, fast as fast_gate, selection as fast_select, verdict as gate_verdict
from tools.gate.tests.base import GateTestCase, fixture_git
from tools.gate.tests.fast_runs import Recorder, everywhere, run_main

TREE = "1" * 40
BASE_TREE = "2" * 40
BASE = "3" * 40


@tag("source", "tree")
class FastGatePassRecordTests(GateTestCase):
    """A passing run on a tree that held still records the tree when the run proves it: the whole suite ran, or the
    base's tree passed and the run selected what changed from it."""

    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "severino" / "fast-passes"

    def run_fast(
        self, *, still=True, ok=True, whole=False, labels=("hq.example.tests.test_x",), **fakes
    ) -> tuple[list[str], list[str]]:
        """(the trees recorded, what the run printed)."""

        total = len(labels) if whole else len(labels) + 5
        selected = fast_gate.quick_gates() if whole else ()
        groups = tuple(fast_select.group_tests().values()) if whole else ()
        result = gate_verdict.Step("tests", ok=ok)
        with everywhere("passes", return_value=self.folder):
            _, printed = run_main(
                selected=selected,
                group_tasks=groups,
                step=lambda label, cmd, *_: result if fast_gate.TESTS in cmd else gate_verdict.Step(label),
                affected=(set(), set()),
                test_labels=(list(labels), total),
                judged=TREE if still else None,
                tree_of=BASE_TREE,
                touches_shared_ui=False,
                **fakes,
            )
        recorded = sorted(path.name for path in self.folder.iterdir()) if self.folder.is_dir() else []
        return recorded, printed

    def test_a_passing_run_of_the_whole_suite_on_a_tree_that_held_still_records_it(self):
        self.assertEqual(self.run_fast(whole=True)[0], [TREE])

    def test_a_run_runs_its_selection_however_wide_and_the_whole_suite_only_when_asked(self):
        """A worker's run never widens to the whole suite; the landing asks for it (--whole), and that pass records
        the tree and starts the chain."""
        suite = [f"hq.example.tests.test_{n}" for n in range(10)]
        for asked, whole in (({}, False), ({"whole": "true"}, True)):
            with self.subTest(asked=asked):
                shutil.rmtree(self.folder, ignore_errors=True)
                recorder = Recorder()
                with everywhere("passes", return_value=self.folder):
                    run_main(
                        ("hq/example.py",),
                        selected=(),
                        step=recorder,
                        asked=asked,
                        affected=(set(), set()),
                        test_labels=(suite[:9], len(suite)),
                        whole_suite=suite,
                        judged=TREE,
                        tree_of=BASE_TREE,
                        touches_shared_ui=False,
                    )
                ran = [word for _, cmd in recorder.calls for word in cmd]
                every_gate = {*fast_gate.quick_gates(), *fast_select.group_tests().values()}
                self.assertEqual(every_gate <= set(ran), whole)
                self.assertEqual(set(suite) <= set(ran), whole)
                self.assertLessEqual(set(suite[:9]), set(ran))
                recorded = sorted(path.name for path in self.folder.iterdir()) if self.folder.is_dir() else []
                self.assertEqual(recorded, [TREE] if whole else [])

    def test_a_passing_run_over_a_passed_base_records_the_tree(self):
        self.folder.mkdir(parents=True)
        (self.folder / BASE_TREE).touch()
        self.assertEqual(self.run_fast()[0], sorted([BASE_TREE, TREE]))

    def test_a_tree_changed_in_the_run_or_a_failed_run_records_nothing(self):
        for case in ({"still": False}, {"ok": False}):
            with self.subTest(**case):
                self.assertEqual(self.run_fast(whole=True, **case)[0], [])

    def test_a_selection_from_a_base_that_passed_nowhere_records_nothing_and_says_why_in_one_line(self):
        """A clean branch equal to its base selects nothing it changed, so its pass says nothing about the tree;
        what CI said of the base is the reason, and the run still passes."""
        for labels in ((), ("hq.example.tests.test_x",)):
            with self.subTest(labels=labels):
                recorded, printed = self.run_fast(labels=labels, passed_in_ci="CI has no completed run on 333333333")
                self.assertEqual(recorded, [])
                (said,) = [line for line in printed if f"tree {TREE[:9]}" in line]
                self.assertIn("not recorded", said)
                self.assertIn("CI has no completed run on 333333333", said)
                self.assertIn("passed in", printed[-1])

    def test_a_base_ci_passed_records_its_tree_so_the_next_run_chains_without_asking(self):
        recorded, printed = self.run_fast(passed_in_ci=None)
        self.assertEqual(recorded, sorted([BASE_TREE, TREE]))
        self.assertTrue(any("recorded as passed" in line and f"tree {TREE[:9]}" in line for line in printed))
        # Asked again, GitHub's answer would leave the tree unrecorded.
        (self.folder / TREE).unlink()
        self.assertEqual(self.run_fast(passed_in_ci="asked GitHub again")[0], sorted([BASE_TREE, TREE]))

    def test_a_whole_run_never_asks_github(self):
        recorded, _ = self.run_fast(whole=True, passed_in_ci="must not be read")
        self.assertEqual(recorded, [TREE])

    def test_the_record_is_shared_by_every_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, other = Path(directory) / "repo", Path(directory) / "other"
            repo.mkdir()
            git = fixture_git(repo)
            git("init", "-q")
            (repo / "a.txt").write_text("a\n")
            git("add", "a.txt")
            git("commit", "--quiet", "--no-verify", "-m", "a")
            git("worktree", "add", "-q", str(other))
            self.assertEqual(fast_chain.passes(other), fast_chain.passes(repo))
            self.assertEqual(fast_chain.passes(repo), (repo / ".git").resolve().joinpath("severino", "fast-passes"))


def workflow_run(conclusion: str, *, fork: bool = False) -> dict[str, object]:
    """A completed run as GitHub's "List workflow runs for a workflow" lists it, the fields read here."""

    own = {"full_name": "example/hq"}
    return {
        "id": 1,
        "head_sha": BASE,
        "event": "pull_request",
        "status": "completed",
        "conclusion": conclusion,
        "path": ".github/workflows/ci.yml",
        "head_repository": {"full_name": "someone/hq"} if fork else own,
        "repository": own,
    }


@tag("source")
class PassedInCiTests(GateTestCase):
    """CI's last completed run on this repository's own commit is the answer; anything else says why, and no
    answer from GitHub is never a failure of the run."""

    def ask(
        self,
        *,
        payload=None,
        code=0,
        stderr="",
        raised: BaseException | None = None,
        commit: str = BASE,
        sha: str = BASE,
    ) -> str | None:
        def run(cmd, **_):
            self.assertEqual(cmd[:2], ["gh", "api"])
            self.assertIn(f"head_sha={sha}", cmd)
            if raised:
                raise raised
            return SimpleNamespace(returncode=code, stdout=json.dumps(payload or {}), stderr=stderr)

        with everywhere("subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)):
            return fast_chain.passed_in_ci(commit)

    def runs(self, *runs: dict[str, object]) -> dict[str, object]:
        return {"total_count": len(runs), "workflow_runs": list(runs)}

    def test_the_newest_run_of_its_own_passed(self):
        self.assertIsNone(self.ask(payload=self.runs(workflow_run("success"), workflow_run("failure"))))

    def test_a_newest_run_that_did_not_pass_says_how_it_ended(self):
        said = self.ask(payload=self.runs(workflow_run("failure"), workflow_run("success")))
        self.assertIn("failure", said or "")

    def test_a_fork_run_never_counts(self):
        self.assertIn("no completed run", self.ask(payload=self.runs(workflow_run("success", fork=True))) or "")

    def test_a_commit_github_lacks_has_no_run(self):
        self.assertIn("no completed run", self.ask(payload=self.runs()) or "")

    def test_no_answer_from_github_is_said_in_one_line(self):
        for case, said in (
            (
                "gh failed",
                self.ask(code=1, stderr="error connecting to api.github.com\ncheck your internet connection"),
            ),
            ("timed out", self.ask(raised=subprocess.TimeoutExpired("gh", 30))),
            ("no gh", self.ask(raised=FileNotFoundError("gh"))),
        ):
            with self.subTest(case=case):
                self.assertTrue(said)
                assert said
                self.assertNotIn("\n", said)

    def test_a_ref_is_asked_for_by_its_whole_hash(self):
        """GitHub finds a run by the whole hash alone, and a base may be named shorter."""
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fast_chain.ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertIsNone(self.ask(payload=self.runs(workflow_run("success")), commit=head[:9], sha=head))
