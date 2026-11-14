"""A gate run on the second machine is asked for as it was asked here, reads as
a run here, and leaves a pass here only for the tree it was sent; a machine
that does not answer leaves the run here."""

import importlib
import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest import mock

from tools.gate import chain
from tools.gate.tests.base import GateTestCase, fixture_git

TREE = "1" * 40
OTHER = "2" * 40
# A usage spec as `mise tasks info --json` lists it (usage_spec.cmd), with only the fields read.
SPEC = {
    "flags": [
        {"name": "at", "long": ["at"], "short": [], "arg": {"name": "ref"}},
        {"name": "keep", "long": ["keep"], "short": [], "var": True, "arg": {"name": "path"}},
        {"name": "fallback-face", "long": ["fallback-face"], "short": []},
        {"name": "go", "long": ["go"], "short": []},
    ],
    "args": [{"name": "arguments", "var": True, "double_dash": "Automatic"}],
}


def archive(files: dict[str, str]) -> bytes:
    kept = io.BytesIO()
    with tarfile.open(fileobj=kept, mode="w") as written:
        for name, text in files.items():
            data = text.encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            written.addfile(entry, io.BytesIO(data))
    return kept.getvalue()


class RemoteTestCase(GateTestCase):
    def setUp(self):
        super().setUp()
        # Imported as each test runs, so a run at a ref without it fails the test rather than the module.
        self.remote = importlib.import_module("tools.gate.remote")


class WordsTests(RemoteTestCase):
    def test_the_run_there_is_handed_the_arguments_mise_parsed_here(self):
        """Each flag with its values, a repeated one once for each, a switch only when given, and the
        variadic words after `--`, so a label's own options and quoting reach the run as typed."""

        values = {
            "at": "abc123",
            "keep": "'a b.py' c.py",
            "fallback_face": "true",
            "arguments": "hq.x.tests --parallel 1 'two words'",
        }
        self.assertEqual(
            self.remote.words(SPEC, values),
            [
                *("--at", "abc123", "--keep", "a b.py", "--keep", "c.py", "--fallback-face"),
                *("--", "hq.x.tests", "--parallel", "1", "two words"),
            ],
        )
        self.assertEqual(self.remote.words(SPEC, {}), [])


class AsHereTests(RemoteTestCase):
    def test_a_line_from_there_names_this_checkout_and_takes_mises_prefix_here_once(self):
        where, home = "/home/example/.cache/hq-gates/" + TREE, "/home/example"
        for written in (where, f"~/.cache/hq-gates/{TREE}"):
            with self.subTest(written=written):
                line = f"[fast] [lint:ruff] {written}/hq/x.py:3: E501\n".encode()
                self.assertEqual(
                    self.remote.as_here(line, "fast", where, home),
                    f"[lint:ruff] {self.remote.ROOT}/hq/x.py:3: E501\n".encode(),
                )


class RecordedElsewhereTests(RemoteTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.passes = self.root / "passes"
        self.enterContext(mock.patch.object(chain, "passes", return_value=self.passes))
        self.enterContext(mock.patch.object(chain, "maps", return_value=self.root / "maps"))
        self.enterContext(mock.patch.object(self.remote, "ROOT", self.root / "checkout"))

    def test_a_pass_there_is_recorded_here_only_for_the_tree_its_verdict_names(self):
        self.assertFalse(chain.record_elsewhere(TREE, f"tree {OTHER}\n[fast] passed\n", "example-host"))
        self.assertFalse((self.passes / TREE).exists())
        self.assertTrue(chain.record_elsewhere(TREE, f"tree {TREE}\n[fast] passed\n", "example-host"))
        self.assertEqual((self.passes / TREE).read_text().strip(), "example-host")

    def test_what_comes_back_is_kept_only_where_its_verdict_names_the_tree_sent(self):
        """A verdict on another tree stays there, and so does its pass; one on the tree sent replaces this
        checkout's, kept beside it as the run before, names this checkout where it named the worktree there,
        and brings its pass and map."""

        gate = self.remote.ROOT / "var" / "gate"
        gate.mkdir(parents=True)
        (gate / "fast.txt").write_text(f"tree {OTHER}\nbefore\n")
        back = {"var/back/fast-passes/" + TREE: "", f"var/back/fast-maps/{TREE}.json": "{}"}
        where = "/home/example/.cache/hq-gates/" + TREE
        sent = self.remote.Sent(TREE, where, "/home/example", [], "", "")
        self.remote.bring_back(archive({"var/gate/fast.txt": f"tree {OTHER}\n", **back}), sent, "example-host", "fast")
        self.assertEqual((gate / "fast.txt").read_text(), f"tree {OTHER}\nbefore\n")
        self.assertFalse((self.passes / TREE).exists())

        shot = "var/look/run/estate.png"
        failed = f"tree {TREE}\n[fast] FAILED: {{}}/hq/x.py:3\n"
        files = {"var/gate/fast.txt": failed.format(where), "var/gate/fast.log": where, shot: "png", **back}
        self.remote.bring_back(archive(files), sent, "example-host", "fast")
        self.assertEqual((gate / "fast.txt").read_text(), failed.format(self.remote.ROOT))
        self.assertEqual((gate / "fast.log").read_text(), str(self.remote.ROOT))
        self.assertEqual((gate / "fast.prev.txt").read_text(), f"tree {OTHER}\nbefore\n")
        self.assertTrue((self.remote.ROOT / shot).is_file())
        self.assertEqual((self.passes / TREE).read_text().strip(), "example-host")
        self.assertTrue((self.root / "maps" / f"{TREE}.json").is_file())


class StaysHereTests(RemoteTestCase):
    def run_main(self, ssh_status: int, **environ: str) -> tuple[int, list[list[str]]]:
        calls: list[list[str]] = []

        def ran(command, **_options):
            calls.append(command)
            return subprocess.CompletedProcess(command, ssh_status, b"", b"")

        base = {name: value for name, value in os.environ.items() if not name.startswith("usage_")}
        given = {"MISE_TASK_NAME": "fast", self.remote.SETTING: "example-host:hq"} | environ
        with (
            mock.patch.dict(os.environ, base | given, clear=True),
            mock.patch.object(self.remote.subprocess, "run", ran),
            mock.patch.object(self.remote, "inside", return_value=False),
            mock.patch.object(self.remote, "run_there", return_value=0) as there,
        ):
            status = self.remote.main()
        return status, calls if not there.called else [*calls, ["there"]]

    def test_a_machine_that_does_not_answer_leaves_the_run_here_after_one_line(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as said:
            status, calls = self.run_main(255)
        self.assertEqual(status, self.remote.HERE)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-2:], ["example-host", "true"])
        self.assertEqual(len(said.getvalue().splitlines()), 1)
        self.assertIn("example-host", said.getvalue())

    def test_a_machine_that_answers_takes_the_run(self):
        self.assertEqual(self.run_main(0)[1][-1], ["there"])

    def test_a_run_that_acts_on_this_machine_never_asks(self):
        for flag in sorted(self.remote.STAYS):
            with self.subTest(flag=flag):
                self.assertEqual(self.run_main(0, **{f"usage_{flag}": "true"}), (self.remote.HERE, []))


class HostHalfTests(RemoteTestCase):
    def test_a_tree_gets_a_worktree_of_its_commit_and_back_archives_only_what_the_run_wrote(self):
        """No ref sent is left behind, the gitignored files .worktreeinclude names are copied in, and the
        archive holds what the run wrote since it began, never an older verdict."""

        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = scratch / "hq"
        checkout.mkdir()
        git = fixture_git(checkout)
        git("init", "--quiet")
        (checkout / ".worktreeinclude").write_text("# copied\nlocal.toml\n")
        git("add", ".")
        git("commit", "--quiet", "--no-verify", "-m", "first")
        (checkout / "local.toml").write_text("kept")
        commit, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
        git("update-ref", f"refs/gate/{tree}", commit)

        def step(*args: str, given: bytes = b"") -> bytes:
            env = os.environ | {"XDG_CACHE_HOME": str(scratch / "cache")}
            command = ["sh", str(self.remote.HOST_HALF), *args]
            return subprocess.run(command, input=given, env=env, capture_output=True, check=True).stdout

        where = Path(step("prepare", str(checkout), tree, commit, given=archive({})).decode().splitlines()[0])
        self.assertEqual(fixture_git(where)("rev-parse", "HEAD"), commit)
        self.assertEqual((where / "local.toml").read_text(), "kept")
        self.assertEqual(git("for-each-ref", "refs/gate"), "")

        (where / "var" / "gate").mkdir(parents=True)
        (where / "var" / "gate" / "old.txt").write_text("before")
        os.utime(where / "var" / "gate" / "old.txt", (0, 0))
        (where / "var" / "remote-stamp").touch()
        os.utime(where / "var" / "remote-stamp", (1000, 1000))
        (where / "var" / "gate" / "fast.txt").write_text(f"tree {tree}\n")
        with tarfile.open(fileobj=io.BytesIO(step("back", tree))) as kept:
            self.assertEqual(kept.getnames(), ["var/gate/fast.txt"])
