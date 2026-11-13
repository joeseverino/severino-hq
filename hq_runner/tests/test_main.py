"""``python -m hq_runner`` works every queued run, each fetched at its pinned
commit and reached through its machines' runner connections, until none is
left; it reads where everything is from its environment alone, and leaves
nothing behind in its scratch space.

Every pass runs over the fake bridge and runner (``fakes``), with procedures
fetched from a repository on this machine (``remote``).
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from . import fakes
from .remote import REPOSITORY, Remote

PLAY = "- hosts: all\n  gather_facts: false\n  tasks: []\n"


class DrainTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.remote = Remote(self.root / "remote")
        self.revision = self.remote.commit({"playbooks/restart.yml": PLAY})
        (self.root / "scratch").mkdir()
        (self.root / "state").mkdir()
        self.connections = self.root / "connections.json"
        fakes.write_connections(self.connections, fakes.connection(fakes.APP, role="runner"))
        self.token = self.root / "token"
        self.token.write_text("ghs_example0123456789token\n", encoding="utf-8")
        self.environ = {
            "HQ_AUTOMATION_ID": fakes.WORKER,
            "HQ_AUTOMATION_BRIDGE_SOCKET": str(self.root / "bridge" / "bridge.sock"),
            "HQ_AUTOMATION_JOURNAL": str(self.root / "state" / "journal.json"),
            "HQ_AUTOMATION_CONNECTIONS": str(self.connections),
            "HQ_AUTOMATION_SSH_DIR": str(self.root / "ssh"),
            "HQ_AUTOMATION_SCRATCH": str(self.root / "scratch"),
            "HQ_AUTOMATION_REPOSITORY": REPOSITORY,
            "HQ_AUTOMATION_TOKEN_FILE": str(self.token),
        }

    def config(self, **changed: str):
        import dataclasses

        from hq_runner.__main__ import Config

        config = Config.read({**self.environ, **changed})
        return dataclasses.replace(config, project=dataclasses.replace(config.project, origin=self.remote.origin))

    def drain(self, bridge: fakes.Bridge, runner: fakes.Runner) -> int:
        from hq_runner.__main__ import drain

        with contextlib.redirect_stdout(io.StringIO()):
            return drain(self.config(), bridge, runner=lambda scratch: runner)

    def test_every_queued_run_is_worked_until_none_is_left(self):
        first, second = fakes.pending(revision=self.revision), fakes.pending(revision=self.revision)
        second["operation"]["id"] = "11111111-2222-4333-8444-666666666666"
        bridge = fakes.Bridge(first, second)
        runner = fakes.Runner(fakes.WELL)

        self.assertEqual(self.drain(bridge, runner), 2)

        self.assertEqual([report["success"] for report in bridge.reports], [True, True])
        self.assertEqual([action for action, _ in bridge.calls].count("claim"), 3)
        (run, _) = runner.runs
        self.assertEqual(run.procedure.revision, self.revision)
        self.assertTrue(run.procedure.project_dir.is_relative_to((self.root / "scratch").resolve()))
        self.assertEqual(run.procedure.playbook, fakes.PLAYBOOK)
        self.assertEqual(run.targets[0].identity, self.root / "ssh" / fakes.APP)
        self.assertEqual(list((self.root / "scratch").iterdir()), [])

    def test_a_host_without_a_runner_connection_is_refused_before_it_runs(self):
        answer = fakes.pending(revision=self.revision)
        answer["input"]["hosts"] = ["other.example.com"]
        bridge = fakes.Bridge(answer)
        runner = fakes.Runner()

        self.drain(bridge, runner)

        self.assertEqual(runner.runs, [])
        self.assertEqual(bridge.reports[0]["conditions"][0]["reason"], "NoConnection")

    def test_the_token_and_the_key_path_never_reach_a_report(self):
        leaky = fakes.recorded("Fail", "runner_on_failed")
        key = str(self.root / "ssh" / fakes.APP)
        leaky["event_data"]["res"]["msg"] = f"ghs_example0123456789token was refused for {key}"
        bridge = fakes.Bridge(fakes.pending(revision=self.revision))

        self.drain(bridge, fakes.Runner([leaky], status="failed"))

        sent = json.dumps(bridge.reports)
        self.assertNotIn("ghs_example0123456789token", sent)
        self.assertNotIn(key, sent)

    def test_a_missing_or_relative_location_is_refused_before_anything_is_claimed(self):
        from hq_runner.__main__ import ConfigError

        for name, value in (
            ("HQ_AUTOMATION_JOURNAL", ""),
            ("HQ_AUTOMATION_SCRATCH", "scratch"),
            ("HQ_AUTOMATION_REPOSITORY", ""),
        ):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                self.config(**{name: value})
