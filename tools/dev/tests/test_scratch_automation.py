"""The worker beside a scratch server reaches the machine it runs on alone, is given
nothing of HQ's own, starts when HQ rings, and stops with the server."""

import os
import tempfile
from pathlib import Path
from unittest import mock

import yaml
from django.test import SimpleTestCase, tag

from hq.platform.application.automation_adoption import REFUSED_KINDS
from hq.platform.application.cadence import AUTOMATION_DOORBELL
from hq.platform.application.machines import Machine
from hq.platform.application.tests.reads import reads
from hq_runner import catalog, connections
from hq_runner.__main__ import Config
from tools.dev import scratch_automation
from tools.dev.scratch_automation import PROFILES, State


def _directory(test: SimpleTestCase) -> Path:
    made = tempfile.TemporaryDirectory()
    test.addCleanup(made.cleanup)
    return Path(made.name)


class ReachTests(SimpleTestCase):
    def test_each_machine_is_reached_as_each_profile_on_loopback_alone(self):
        root = _directory(self)
        path = root / "connections.json"
        scratch_automation.write_connections(path, scratch_automation.connections(["alpha", "beta", "alpha"]))
        reached = connections.runner_targets(path, root / "ssh")
        self.assertEqual(set(reached), {(machine, profile) for machine in ("alpha", "beta") for profile in PROFILES})
        self.assertEqual({target.host for target in reached.values()}, {"127.0.0.1"})

    def test_the_worker_reaches_the_machine_this_server_runs_on_alone(self):
        catalog = [Machine("app.example.com"), Machine("dev.example.com", runs_hq=True), Machine("db.example.com")]

        self.assertEqual(scratch_automation.here(catalog), ["dev.example.com"])
        self.assertEqual(scratch_automation.here(catalog[:1]), [])

    def test_a_rewrite_replaces_the_document_the_worker_trusts(self):
        root = _directory(self)
        path = root / "connections.json"
        scratch_automation.write_connections(path, scratch_automation.connections(["alpha"]))
        scratch_automation.write_connections(path, scratch_automation.connections(["beta"]))
        self.assertEqual({machine for machine, _ in connections.runner_targets(path, root)}, {"beta"})

    def test_the_worker_is_given_its_own_variables_and_nothing_of_hqs(self):
        state = State.of("/example/var/dev/scratch/db.sqlite3")
        given = state.environment("/example/bridge/bridge.sock")
        self.assertEqual({name for name in given if not name.startswith("HQ_AUTOMATION_")}, set())
        self.assertEqual(Config.read(given).bridge_socket, "/example/bridge/bridge.sock")

    def test_it_waits_on_the_doorbell_hq_rings_beside_the_database(self):
        state = State.of("/example/var/dev/scratch/db.sqlite3")
        self.assertEqual(state.doorbell, Path("/example/var/dev/scratch") / AUTOMATION_DOORBELL)


@tag("source", "tree")
@reads("tools/dev/scratch_playbooks/**")
class PlaybookTests(SimpleTestCase):
    def test_every_play_runs_on_this_machine(self):
        for path, text in scratch_automation.playbook_files().items():
            if not path.startswith("playbooks/") or path.endswith(catalog.SIDECAR):
                continue
            for play in yaml.safe_load(text):
                with self.subTest(path=path, play=play.get("name")):
                    self.assertEqual(play.get("vars", {}).get("ansible_connection"), "local")

    def test_every_privilege_a_playbook_names_has_a_login(self):
        for path, text in scratch_automation.playbook_files().items():
            if path.endswith(catalog.SIDECAR):
                privilege = yaml.safe_load(text).get("privilege", "")
                self.assertIn(connections.profile(privilege), PROFILES, path)

    def test_nothing_in_the_playbooks_keeps_their_commit_from_being_adopted(self):
        refused = [
            found for found in catalog.unpinned(scratch_automation.playbook_files()) if found["kind"] in REFUSED_KINDS
        ]
        self.assertEqual(refused, [])


@tag("source")
class WatchTests(SimpleTestCase):
    def watched(self, *, served: bool, rung: bool, alive: list[bool]):
        root = _directory(self)
        database = root / "scratch" / "db.sqlite3"
        database.parent.mkdir()
        bridge = root / "bridge"
        bridge.mkdir(mode=0o700)
        if served:
            (bridge / "bridge.sock").touch()
        if rung:
            (database.parent / AUTOMATION_DOORBELL).touch()
        worker = mock.Mock(**{"poll.return_value": None})
        environment = {"SEVERINO_DATABASE_PATH": str(database), "SEVERINO_BRIDGE_SOCKET": str(bridge / "bridge.sock")}
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(scratch_automation, "server_alive", side_effect=alive),
            mock.patch.object(scratch_automation.time, "sleep"),
            mock.patch.object(scratch_automation, "start_worker", return_value=worker) as start,
        ):
            scratch_automation.watch(os.getpid(), "/example/python")
        return start, worker, bridge

    def test_a_doorbell_starts_the_worker_and_the_servers_end_stops_it(self):
        start, worker, bridge = self.watched(served=True, rung=True, alive=[True, True, False])
        start.assert_called_once()
        worker.terminate.assert_called_once()
        self.assertFalse(bridge.exists())

    def test_a_doorbell_waits_for_the_bridge(self):
        start, _worker, bridge = self.watched(served=False, rung=True, alive=[True, True, False])
        start.assert_not_called()
        self.assertFalse(bridge.exists())

    def test_no_doorbell_starts_nothing(self):
        start, _worker, _bridge = self.watched(served=True, rung=False, alive=[True, False])
        start.assert_not_called()
