"""A gate run on the second machine is asked for as it was asked here, with
every setting of this run that changes what a gate does, reads as a run here,
and leaves a pass here only for the tree it was sent; a machine that does not
answer leaves the run here."""

import importlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest import mock

from django.test import tag

from hq.platform.application.tests.reads import reads
from hq.platform.core.tests.task import names
from hq.platform.core.tests.test_settings_env import environment_reads
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
        # The turn reads the machine's budget as it loads, before a test stands in for the programs it runs.
        importlib.import_module("tools.gate.turn")


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
        sent = self.remote.Sent(TREE, OTHER, [], "run", None, "")
        there = self.remote.Where(where, "/home/example")
        self.remote.bring_back(
            archive({"var/gate/fast.txt": f"tree {OTHER}\n", **back}), sent, there, "example-host", "fast"
        )
        self.assertEqual((gate / "fast.txt").read_text(), f"tree {OTHER}\nbefore\n")
        self.assertFalse((self.passes / TREE).exists())

        shot = "var/look/run/estate.png"
        failed = f"tree {TREE}\n[fast] FAILED: {{}}/hq/x.py:3\n"
        files = {"var/gate/fast.txt": failed.format(where), "var/gate/fast.log": where, shot: "png", **back}
        self.remote.bring_back(archive(files), sent, there, "example-host", "fast")
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
            return subprocess.CompletedProcess(command, ssh_status, b"4\n4\n" * (not ssh_status), b"")

        base = {name: value for name, value in os.environ.items() if not name.startswith("usage_")}
        state = self.enterContext(tempfile.TemporaryDirectory())
        given = {"MISE_TASK_NAME": "fast", self.remote.SETTING: "example-host:hq", "MISE_STATE_DIR": state}
        with (
            mock.patch.dict(os.environ, base | given | environ, clear=True),
            mock.patch.object(self.remote.subprocess, "run", ran),
            mock.patch.object(self.remote, "inside", return_value=False),
            mock.patch.object(self.remote.room, "room", return_value=(0, 4, set())),
            mock.patch.object(self.remote, "run_there", return_value=0) as there,
            mock.patch.object(sys, "argv", ["remote", "all"]),
        ):
            status = self.remote.main()
        return status, calls if not there.called else [*calls, ["there"]]

    def test_a_machine_that_does_not_answer_leaves_the_run_here_after_one_line(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as said:
            status, calls = self.run_main(255)
        self.assertEqual(status, self.remote.HERE)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][: len(self.remote.SSH) + 1], [*self.remote.SSH, "example-host"])
        self.assertEqual(len(said.getvalue().splitlines()), 1)
        self.assertIn("example-host", said.getvalue())

    def test_a_machine_that_answers_with_room_takes_the_run(self):
        self.assertEqual(self.run_main(0)[1][-1], ["there"])

    def test_a_run_that_acts_on_this_machine_never_asks(self):
        for flag in sorted(self.remote.STAYS):
            with self.subTest(flag=flag):
                self.assertEqual(self.run_main(0, **{f"usage_{flag}": "true"}), (self.remote.HERE, []))


class ChoiceTests(RemoteTestCase):
    """A run starts on whichever machine has the tokens it takes free now, the second machine first, and waits
    only when neither has, saying on which machine and why. The two machines' rooms are stand-ins."""

    def choose(self, demand: str, here: tuple[int, int], there: tuple[int, int] | None) -> tuple[bool, str]:
        """Whether a run weighed at ``demand`` went there, and what it said, with this machine's free tokens
        and capacity ``here`` and the second machine's ``there`` (None: it does not answer)."""

        state = self.enterContext(tempfile.TemporaryDirectory())
        given = {"MISE_TASK_NAME": "test", self.remote.SETTING: "example-host:hq", "MISE_STATE_DIR": state}
        answer = None if there is None else self.remote.Room(*there)
        with (
            mock.patch.dict(os.environ, given),
            mock.patch.object(self.remote, "inside", return_value=False),
            mock.patch.object(self.remote, "asked", return_value=answer),
            mock.patch.object(self.remote.room, "room", return_value=(*here, set())),
            mock.patch.object(self.remote, "run_there", return_value=0) as ran,
            mock.patch.object(sys, "argv", ["remote", demand]),
            mock.patch("sys.stderr", new_callable=io.StringIO) as said,
        ):
            status = self.remote.main()
        self.assertEqual(status, 0 if ran.called else self.remote.HERE)
        return ran.called, said.getvalue()

    def test_a_run_that_fits_here_while_the_second_machine_is_busy_runs_here(self):
        went, said = self.choose("2", here=(4, 4), there=(0, 8))
        self.assertFalse(went)
        self.assertIn("runs here", said)
        self.assertIn("0 of 8", said)

    def test_a_run_goes_there_when_that_machine_has_room_for_it(self):
        for demand, there in (("2", (2, 8)), ("1", (1, 8)), ("all", (8, 8)), ("4", (8, 8))):
            with self.subTest(demand=demand, there=there):
                self.assertEqual(self.choose(demand, here=(4, 4), there=there), (True, ""))

    def test_a_run_neither_machine_has_room_for_waits_on_one_and_says_which_and_why(self):
        """It waits on the machine that lacks the fewest tokens, the second machine on a tie, and its line
        names that machine, the tokens the run takes and what each machine has free."""

        went, said = self.choose("2", here=(0, 4), there=(1, 8))
        self.assertTrue(went)
        self.assertRegex(said, r"waits on example-host\b")
        self.assertIn("2 tokens", said)
        self.assertIn("1 of 8", said)
        self.assertIn("0 of 4", said)
        went, said = self.choose("all", here=(3, 4), there=(2, 8))
        self.assertFalse(went)
        self.assertIn("waits on this machine", said)
        self.assertIn("every token", said)

    def test_a_weight_that_takes_every_token_here_takes_every_token_there(self):
        """A parallel run of a tree is weighed at this machine's whole budget, which on a larger machine is the
        whole of it too, never a part of it."""

        self.assertFalse(self.choose("4", here=(4, 4), there=(6, 8))[0])
        self.assertTrue(self.choose("3", here=(4, 4), there=(3, 8))[0])

    def test_a_machine_that_cannot_say_its_room_leaves_the_run_here(self):
        went, said = self.choose("1", here=(0, 4), there=None)
        self.assertFalse(went)
        self.assertEqual(len(said.splitlines()), 1)

    def test_runs_started_together_count_the_tokens_sent_ahead_of_them(self):
        """A run sent there holds its tokens until its share there does, so a run choosing while the first is
        on its way counts them, and stops counting them once the room there names the first run's worktree."""

        state = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {"MISE_STATE_DIR": state}))
        first = self.remote.Claim("example-host", 6)
        try:
            self.assertEqual(self.remote.claimed("example-host", frozenset()), 6)
            self.assertEqual(self.remote.claimed("elsewhere.example", frozenset()), 0)
            first.took("/home/example/.cache/hq-gates/sender-1")
            self.assertEqual(self.remote.claimed("example-host", frozenset()), 6)
            self.assertEqual(
                self.remote.claimed("example-host", frozenset({"/home/example/.cache/hq-gates/sender-1"})), 0
            )
        finally:
            first.close()
        self.assertEqual(self.remote.claimed("example-host", frozenset()), 0)
        # A claim its sender left behind when it died holds nothing.
        left = self.remote.Claim("example-host", 2)
        os.close(left.descriptor)
        self.assertEqual(self.remote.claimed("example-host", frozenset()), 0)
        self.assertFalse(left.path.exists())

    def test_a_run_choosing_behind_one_sent_ahead_runs_here_when_the_tokens_left_there_are_too_few(self):
        state = self.enterContext(tempfile.TemporaryDirectory())
        given = {"MISE_TASK_NAME": "dev:look", self.remote.SETTING: "example-host:hq", "MISE_STATE_DIR": state}
        self.enterContext(mock.patch.dict(os.environ, given))
        first = self.remote.Claim("example-host", 8)
        self.addCleanup(first.close)
        with (
            mock.patch.object(self.remote, "inside", return_value=False),
            mock.patch.object(self.remote, "asked", return_value=self.remote.Room(8, 8)),
            mock.patch.object(self.remote.room, "room", return_value=(4, 4, set())),
            mock.patch.object(self.remote, "run_there", return_value=0) as ran,
            mock.patch.object(sys, "argv", ["remote", "2"]),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(self.remote.main(), self.remote.HERE)
        self.assertFalse(ran.called)


# flock(1) for a machine without util-linux's (macOS): it locks the descriptor it is named, as flock(2) locks
# the open file the shell holds there, so the lock lasts as long as the shell's descriptor.
FLOCK = """#!PYTHON
import fcntl, sys
try:
    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
"""


class HostHalfTests(RemoteTestCase):
    def setUp(self):
        super().setUp()
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkout = self.scratch / "hq"
        self.checkout.mkdir()
        self.git = fixture_git(self.checkout)
        self.git("init", "--quiet")
        (self.checkout / ".worktreeinclude").write_text("# copied\nlocal.toml\n")
        (self.checkout / ".gitignore").write_text("local.toml\nvar/\n")
        (self.checkout / "kept.py").write_text("one = 1\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "--no-verify", "-m", "first")
        (self.checkout / "local.toml").write_text("kept")
        self.commit, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        fake = self.scratch / "bin"
        fake.mkdir()
        # macOS has no setsid; the run's own session is not what this holds.
        (fake / "setsid").write_text('#!/bin/sh\nexec "$@"\n')
        if not shutil.which("flock"):
            (fake / "flock").write_text(FLOCK.replace("PYTHON", sys.executable))
        for each in fake.iterdir():
            each.chmod(0o755)
        self.env = os.environ | {"XDG_CACHE_HOME": str(self.scratch / "cache"), "PATH": f"{fake}:{os.environ['PATH']}"}

    def step(self, *args: str, given: bytes = b"") -> bytes:
        command = ["sh", str(self.remote.HOST_HALF), *args]
        return subprocess.run(command, input=given, env=self.env, capture_output=True, check=True).stdout

    def start(self, script: str, run: str = "run", key: str = "sender") -> subprocess.Popen:
        """A run of ``script`` sent as a run's push leaves the tree, its input left open: closing it is what
        stops a run there."""

        self.git("update-ref", f"refs/gate/{run}", self.commit)
        words = [run, key, str(self.checkout), self.tree, self.commit, "-", "", "", "sh", "-c", script]
        command = ["sh", str(self.remote.HOST_HALF), "run", *words]
        return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self.env)

    def ran(self, script: str, run: str = "run") -> tuple[int, Path, str]:
        """The status of a run of ``script``, the worktree it took and what it printed after naming it."""

        with self.start(script, run) as started:
            said = started.stdout.read().decode() if started.stdout else ""
            status = started.wait()
        where, rest = said.split("\n", 1)
        return status, Path(where.split()[0]), rest

    def test_a_tree_gets_a_worktree_of_its_commit_and_back_archives_only_what_the_run_wrote(self):
        """No ref sent is left behind, the gitignored files .worktreeinclude names and the files staged for
        the run are laid in, and back holds what the run wrote, once, never an older verdict."""

        self.step("stage", "run", given=archive({"var/probe.js": "probe"}))
        status, where, said = self.ran('echo "$HQ_GATE_SENT" >var/sent; mkdir -p var/gate; echo ok >var/gate/x.txt')
        self.assertEqual(status, 0, said)
        self.assertEqual(fixture_git(where)("rev-parse", "HEAD"), self.commit)
        self.assertEqual((where / "local.toml").read_text(), "kept")
        self.assertEqual((where / "var" / "probe.js").read_text(), "probe")
        self.assertEqual((where / "var" / "sent").read_text().strip(), self.tree)
        self.assertEqual(self.git("for-each-ref", "refs/gate"), "")
        with tarfile.open(fileobj=io.BytesIO(self.step("back", "run"))) as kept:
            self.assertEqual(kept.getnames(), ["var/gate/x.txt"])
        self.assertEqual(self.step("back", "run"), b"")

    def test_a_checkout_s_next_tree_reuses_its_worktree_judged_as_sent_whatever_an_earlier_run_wrote(self):
        """The environment and caches a run left stay warm for the next tree its checkout sends, while a
        tracked file it rewrote or a new one it left is gone: the run judges the tree sent."""

        from tools.gate.tree import working_tree

        _, where, _ = self.ran("echo 'one = 2' >kept.py; echo 'left = True' >stray.py; echo mine >var/kept.txt")
        status, again, said = self.ran("true", run="again")
        self.assertEqual((status, again), (0, where), said)
        self.assertEqual(working_tree(where), self.tree)
        self.assertEqual((where / "local.toml").read_text(), "kept")
        self.assertEqual((where / "var" / "kept.txt").read_text().strip(), "mine")

    def test_runs_at_once_never_share_a_worktree(self):
        """Two runs of one checkout at once each take a worktree of their own, so neither resets the tree or
        the stamp of what it wrote under the other."""

        with self.start("sleep 60", run="first") as first:
            assert first.stdout and first.stdin
            where = Path(first.stdout.readline().decode().split()[0])
            status, other, said = self.ran("true", run="second")
            first.stdin.close()
            first.stdout.read()
        self.assertEqual(status, 0, said)
        self.assertNotEqual(other, where)

    def test_room_says_the_machines_free_tokens_and_the_count_its_clone_declares(self):
        """The sender weighs a run against that machine by the code that reads its own room, run there in the
        clone whose git config declares the machine's tokens."""

        from tools.gate.room import DECLARED

        self.git("config", DECLARED, "3")
        self.env |= {"MISE_STATE_DIR": str(self.scratch / "state")}
        said = self.step("room", str(self.checkout), given=self.remote.ROOM.read_bytes()).decode()
        self.assertEqual(said.splitlines(), ["3", "3"])

    def test_a_run_that_rewrites_the_tree_it_was_sent_fails_naming_what_it_wrote(self):
        """A step that writes a fix there (the formatter, on a file sent unformatted) passes a tree nobody sent
        and never brings it back, so the run fails, naming the files; one that writes only what git ignores
        passes."""

        status, _, said = self.ran("mkdir -p var/gate; echo ok >var/gate/x.txt")
        self.assertEqual(status, 0, said)
        (self.checkout / "unformatted.py").write_text("one=1\n")
        self.git("add", "unformatted.py")
        self.git("commit", "--quiet", "--no-verify", "-m", "unformatted")
        self.commit, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        ruff = Path(sys.executable).with_name("ruff")
        status, _, said = self.ran(f"{shlex.quote(str(ruff))} format --quiet --no-cache unformatted.py")
        self.assertNotEqual(status, 0)
        self.assertIn("unformatted.py", said)


class ComposedTests(RemoteTestCase):
    def test_every_run_that_may_compose_sends_this_machines_extension_checkout(self):
        """fast runs the composed suite when its change reaches the extensions, so it sends the extension
        checkout HQ_LOCAL_PYTHONPATH names as the composed suite does, never composing that machine's own."""

        composes = self.remote.composes
        self.assertTrue(composes("fast", {}))
        self.assertTrue(composes("test", {"composed": "true"}))
        self.assertFalse(composes("test", {"arguments": "hq.x.tests"}))
        self.assertFalse(composes("dev:look", {}))
        self.assertIn("HQ_LOCAL_PLUGINS", self.remote.CARRIED)

    def test_runs_sending_one_tree_at_once_push_refs_of_their_own(self):
        """Worktrees send the same extension tree at once; a ref named for the tree makes the second push
        fail as the first creates it, and the first run there to take its commit would delete it."""

        pushed = []
        spec = json.dumps({"usage_spec": {"cmd": {}}})
        with (
            mock.patch.dict(os.environ, {"HQ_LOCAL_PYTHONPATH": str(self.remote.ROOT)}),
            mock.patch.object(self.remote, "working_tree", return_value=TREE),
            mock.patch.object(self.remote, "gate_commit", return_value=OTHER),
            mock.patch.object(self.remote, "git", return_value=[str(self.remote.ROOT)]),
            mock.patch.object(self.remote, "push", side_effect=lambda _root, _to, refspecs: pushed.extend(refspecs)),
            mock.patch.object(self.remote, "done", return_value=spec),
            mock.patch.object(self.remote, "words", return_value=[]),
            mock.patch.object(self.remote.chain, "answered", return_value=[]),
            mock.patch.object(self.remote.chain, "fast_base", return_value=(OTHER,)),
            mock.patch.object(self.remote.chain, "passes", return_value=Path("/nonexistent")),
            mock.patch.object(self.remote.chain, "maps", return_value=Path("/nonexistent")),
        ):
            sent = [self.remote.send("fast", "example-host", "/home/example/hq", {}) for _ in range(2)]
        refs = [spec.split(":", 1)[1] for spec in pushed]
        self.assertEqual(len(refs), 4, pushed)
        self.assertNotEqual(refs[0], refs[2], "the tree's ref")
        self.assertNotEqual(refs[1], refs[3], "the extension's ref")
        self.assertNotEqual(sent[0].id, sent[1].id)


class CarriedTests(RemoteTestCase):
    def handed(self, environ: dict[str, str], *, terminal: bool) -> list[str]:
        """The variables the run there is started with, by the command run_there sends."""

        sent = self.remote.Sent(TREE, OTHER, ["--", "x"], "run", None, "")
        screen = mock.Mock(isatty=mock.Mock(return_value=terminal))
        with (
            mock.patch.dict(os.environ, environ, clear=True),
            mock.patch.object(self.remote, "send", return_value=sent),
            mock.patch.object(self.remote, "bring_back"),
            mock.patch.object(self.remote.subprocess, "run"),
            mock.patch.object(self.remote.subprocess, "Popen") as started,
            mock.patch("sys.stdout", screen),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            started.return_value.stdout = [b"/home/example/hq-gates/sender-1 /home/example\n"]
            started.return_value.wait.return_value = 0
            self.remote.run_there("test", "example-host", "hq", {}, lambda _where: None)
        # The host half's arguments are one word of the ssh command line, as the shell there splits it.
        (there,) = [word for word in started.call_args.args[0] if "remote-host.sh" in word]
        words = shlex.split(there)
        return words[words.index("env") + 1 : words.index("mise")]

    def test_the_run_there_has_the_browser_and_the_output_this_run_has(self):
        """An engine named here draws there, and the run there, which writes to no terminal, shows its output as
        this run would."""

        environ = {"PATH": os.environ.get("PATH", ""), "HQ_BROWSER_ENGINE": "chromium", "HOME": "/home/here"}
        self.assertIn("HQ_BROWSER_ENGINE=chromium", self.handed(environ, terminal=False))
        self.assertNotIn("HOME=/home/here", self.handed(environ, terminal=False))
        self.assertIn("HQ_GATE_OUTPUT=all", self.handed(environ, terminal=True))
        self.assertIn("HQ_GATE_OUTPUT=short", self.handed(environ, terminal=False))
        self.assertIn("HQ_GATE_OUTPUT=all", self.handed(environ | {"HQ_GATE_OUTPUT": "all"}, terminal=False))


# The code a gate runs, wherever it runs: mise's tasks and the files they run,
# the gate tools, and the test runner with the harness its browser tests and
# looks are built on. Python is read by what it parses to, the rest by text.
GATE_PYTHON = ("tools/gate", "tools/lib", "tools/perf", "hq/platform/core/test_runner.py", "hq/platform/core/tests")
GATE_TEXT = ("mise.toml", "mise.fast.toml", "mise-tasks", "tools/lib", "tools/gate")
# A read in a shell script, Perl's %ENV or a mise template. A lowercase name
# is a script's own, and a name indexed ($SIG{...}, $ARGV[0]) is Perl's.
TEXT_READ = re.compile(
    r"\$ENV\{(\w+)\}|\$\{?([A-Z][A-Z0-9_]*)\b(?![\[{])|\benv\.([A-Z][A-Z0-9_]*)|get_env\(name=['\"](\w+)"
)


def gate_reads(root: Path, python: tuple[str, ...], text: tuple[str, ...]) -> tuple[dict[str, set[str]], list[str]]:
    """Each environment variable the gate code under ``root`` reads, with the files that read it, and each
    read in Python whose name nothing spells. A tool's own tests are no gate code."""

    top = root.resolve()

    def gate_code(path: Path) -> bool:
        parts = path.relative_to(top).parts
        return "tests" not in parts or not (parts[-1].startswith("test_") or parts[0] == "tools")

    found, unnamed = environment_reads(*(root / path for path in python), kept=gate_code)
    read = {name: {str(path.relative_to(top)) for path in paths} for name, paths in found.items()}
    for listed in names("ls-files", "--cached", "--others", "--exclude-standard", "--", *text, root=root):
        file = top / listed
        if file.suffix == ".py" or not gate_code(file) or not file.is_file():
            continue
        for match in TEXT_READ.finditer(file.read_text(encoding="utf-8")):
            read.setdefault(next(group for group in match.groups() if group), set()).add(listed)
    return read, [f"{each.path.relative_to(top)}:{each.line} {each.text}" for each in unnamed]


@tag("source", "tree")
@reads(
    "mise.toml",
    "mise.fast.toml",
    "mise-tasks/**",
    "tools/gate/**",
    "tools/lib/**",
    "tools/perf/**",
    "hq/platform/core/test_runner.py",
    "hq/platform/core/tests/**",
    "hq/config/sandbox.py",
    lists=("mise-tasks/**", "tools/gate/**", "tools/lib/**", "tools/perf/**", "hq/platform/core/tests/**"),
)
class GateSettingTests(RemoteTestCase):
    """Each variable a gate reads is carried to the run there or left here with the reason, since a setting
    neither names changes a run here and is silently dropped from the same run there."""

    def test_every_variable_a_gate_reads_is_carried_there_or_left_here_with_its_reason(self):
        read, unnamed = gate_reads(chain.ROOT, GATE_PYTHON, GATE_TEXT)
        carried = set(self.remote.CARRIED)
        left = {name for names in getattr(self.remote, "LEFT", {}).values() for name in names}

        self.assertIn("HQ_BROWSER_FACE", read, "the scan read no browser setting, so it checks nothing")
        self.assertEqual(unnamed, [])
        self.assertEqual({name: sorted(by) for name, by in read.items() if name not in carried | left}, {})
        self.assertEqual(sorted((carried | left) - read.keys()), [], "named, but no gate reads it")
        self.assertEqual(sorted(carried & left), [])

    def test_a_read_in_python_a_task_perl_or_a_template_is_found_and_a_test_s_is_not(self):
        """The negative control: a scan that read nothing would pass the test above."""

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        files = {
            "tools/gate/tool.py": "import os\nNAMED = 'EXAMPLE_CONSTANT'\nos.environ.get('EXAMPLE_PY')\nNAMED in os.environ\n",
            "tools/gate/tests/test_tool.py": "import os\nos.environ.get('EXAMPLE_TESTED')\n",
            "tools/gate/tests/support.py": "import os\nos.environ.get('EXAMPLE_SUPPORT')\n",
            "mise-tasks/task": "#!/bin/sh\n#MISE depends=[\"x {{ get_env(name='EXAMPLE_GET') }}\"]\n"
            'echo "${EXAMPLE_SHELL:-}" "$EXAMPLE_BARE" "${own}"\n',
            "mise-tasks/measured": "perl -e 'print $ENV{EXAMPLE_PERL}'\n",
            "mise.toml": '[tasks.x]\nrun = "echo {{ env.EXAMPLE_TERA }}"\n',
        }
        fixture_git(root)("init", "--quiet")
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)

        read, unnamed = gate_reads(root, ("tools/gate",), ("mise.toml", "mise-tasks", "tools/gate"))

        self.assertEqual(
            sorted(read),
            sorted(f"EXAMPLE_{name}" for name in ("PY", "CONSTANT", "GET", "SHELL", "BARE", "PERL", "TERA")),
        )
        self.assertEqual(unnamed, [])
