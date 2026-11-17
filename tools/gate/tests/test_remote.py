"""What a gate sent to the second machine is asked for there: its arguments as they were typed."""

import io
import shlex
import subprocess
from unittest import mock

from django.test import SimpleTestCase

from tools.gate import remote


class RunThereTests(SimpleTestCase):
    def test_an_argument_holding_a_space_arrives_whole(self):
        typed = ["--operation", "POST /api/v2/capabilities/example.write/", "api/docs"]
        sent = remote.Sent("tree", "commit", typed, "example-run", None, "", [])
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
