"""What a gate sent to the second machine is asked for there: its arguments as they were typed, on a HEAD that is the sender's."""

import io
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from tools.gate import remote
from tools.gate.tests.base import fixture_git
from tools.gate.tree import working_tree


class RunThereTests(SimpleTestCase):
    def test_a_negated_flag_arrives_distinct_from_an_omitted_one(self):
        spec = {"flags": [{"name": "perf", "long": ["perf"], "negate": "--no-perf"}], "args": []}
        self.assertEqual(remote.words(spec, {"perf": "false"}), ["--no-perf"])
        self.assertEqual(remote.words(spec, {"perf": "true"}), ["--perf"])
        self.assertEqual(remote.words(spec, {}), [])

    def test_an_argument_holding_a_space_arrives_whole(self):
        typed = ["--operation", "POST /api/v2/capabilities/example.write/", "api/docs"]
        sent = remote.Sent("tree", "commit", "head", typed, "example-run", None, "", [])
        started = mock.Mock(stdout=io.BytesIO(b""), stdin=None)
        started.wait.return_value = 0
        with (
            mock.patch.object(remote, "send", return_value=sent),
            mock.patch.object(remote, "push"),
            mock.patch.object(subprocess, "Popen", return_value=started) as popen,
            mock.patch("sys.stderr", io.StringIO()),
            self.assertRaises(remote.RefError),
        ):
            remote.run_there("dev:look", "example-host", "/srv/example", {}, lambda where: None)

        (command,) = popen.call_args.args
        host = command.index("example-host")
        # ssh joins the words after the host with spaces, and the shell there splits that line again.
        there = shlex.split(" ".join(command[host + 1 :]))
        self.assertEqual(there[-len(typed) :], typed)


class HostHalfTests(SimpleTestCase):
    def test_head_there_is_the_senders_with_the_rest_of_the_tree_uncommitted(self):
        """So a step that reads the change from HEAD (a look's catalog:<sheet>) reads the one it reads here."""

        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = work / "checkout"
        checkout.mkdir()
        git = fixture_git(checkout)
        git("init", "-q")
        (checkout / ".gitignore").write_text("var/\n")
        (checkout / "sheet.css").write_text(".example { color: red; }\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        head = git("rev-parse", "HEAD")
        (checkout / "sheet.css").write_text(".example { color: blue; }\n")
        (checkout / "added.css").write_text(".added { color: red; }\n")
        tree = working_tree(checkout)
        commit = remote.gate_commit(checkout, tree)
        # The second machine's flock and setsid, which a Mac lacks, as the run there uses them.
        shims = work / "bin"
        shims.mkdir()
        for name, body in (("flock", "exit 0"), ("setsid", 'exec "$@"')):
            (shims / name).write_text(f"#!/bin/sh\n{body}\n")
            (shims / name).chmod(0o755)
        environment = os.environ | {
            "PATH": f"{shims}:{os.environ['PATH']}",
            "HOME": str(work),
            "XDG_CACHE_HOME": str(work / "cache"),
        }
        asked = ["example-run", "example-key", str(checkout), tree, commit, head, "-", "", ""]
        reads = "git rev-parse HEAD; git status --porcelain"
        done = subprocess.run(
            ["sh", "-c", remote.HOST_HALF.read_text(), "remote-host.sh", "run", *asked, "sh", "-c", reads],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        _, there, *status = done.stdout.splitlines()
        self.assertEqual(there, head)
        self.assertEqual(sorted(status), [" M sheet.css", "?? added.css"])
