"""A run's machines are reached only through the runner-role connection named
for each, as the inventory hands it to Ansible: its address, port and user,
its own identity alone and its pinned host key, with no agent, password or
other key, and nothing of any other connection in the document.

The documents are written as the root renderer writes them: the shape
``controller/api/hq-connections.openapi.json`` declares, in this account's
own private file.
"""

import shlex
import tempfile
import unittest
from pathlib import Path

from . import fakes


class DocumentTestCase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.ssh_dir = self.root / "ssh"
        self.path = self.root / "connections.json"

    def write(self, *connections: dict, schema_version: int = 2, mode: int = 0o600) -> Path:
        return fakes.write_connections(self.path, *connections, schema_version=schema_version, mode=mode)

    def targets(self) -> dict:
        from hq_runner.connections import runner_targets

        return runner_targets(self.path, self.ssh_dir)


class InventoryTests(DocumentTestCase):
    def test_each_host_is_reached_as_its_runner_connection_says_and_no_other_way(self):
        from hq_runner.ansible import _inventory

        self.write(
            fakes.connection("app.example.com", role="runner", host="192.0.2.10"),
            fakes.connection("edge.example.com", role="runner", host="198.51.100.7", port="7722", user="example-ops"),
        )
        targets = self.targets()

        hosts = ("app.example.com", "edge.example.com")
        inventory = _inventory(hosts, tuple(targets[host] for host in hosts))

        found = inventory["all"]["hosts"]
        self.assertEqual(set(found), set(hosts))
        edge = found["edge.example.com"]
        self.assertEqual(edge["ansible_connection"], "ssh")
        self.assertEqual(edge["ansible_host"], "198.51.100.7")
        self.assertEqual(edge["ansible_port"], 7722)
        self.assertEqual(edge["ansible_user"], "example-ops")
        self.assertEqual(edge["ansible_ssh_private_key_file"], str(self.ssh_dir / "edge.example.com"))
        self.assertTrue(edge["ansible_host_key_checking"])
        self.assertNotIn("ansible_shell_type", edge)
        self.assertEqual(found["app.example.com"]["ansible_port"], 22)
        self.assertEqual(
            found["app.example.com"]["ansible_ssh_private_key_file"], str(self.ssh_dir / "app.example.com")
        )
        words = shlex.split(edge["ansible_ssh_common_args"])
        options = dict(
            option.split("=", 1) for flag, option in zip(words[::2], words[1::2], strict=True) if flag == "-o"
        )
        self.assertEqual(
            options,
            {
                "UserKnownHostsFile": str(self.ssh_dir / "known_hosts"),
                "GlobalKnownHostsFile": "/dev/null",
                "StrictHostKeyChecking": "yes",
                "IdentitiesOnly": "yes",
                "IdentityAgent": "none",
                "BatchMode": "yes",
                "PasswordAuthentication": "no",
                "KbdInteractiveAuthentication": "no",
                "ForwardAgent": "no",
            },
        )

    def test_a_connection_s_shell_type_is_told_to_ansible_only_when_it_carries_one(self):
        from hq_runner.ansible import _inventory

        self.write(fakes.connection("win.example.com", role="runner", shell_type="powershell"))
        (target,) = self.targets().values()

        host = _inventory(("win.example.com",), (target,))["all"]["hosts"]["win.example.com"]

        self.assertEqual(host["ansible_shell_type"], "powershell")

    def bound_run(self):
        from hq_runner.claim import Procedure, Run

        self.write(fakes.connection("app.example.com", role="runner"))
        targets = tuple(self.targets().values())
        return Run(
            "11111111-2222-4333-8444-555555555555",
            2,
            "example.check",
            Procedure("0" * 40, self.root / "project", "playbooks/check.yml"),
            ("app.example.com",),
            targets,
            {},
            60,
        )

    def test_ansible_s_working_files_stay_inside_the_run_s_private_directory(self):
        from hq_runner.ansible import arguments

        private = self.root / "run-example"

        envvars = arguments(self.bound_run(), private)["envvars"]

        for name in ("ANSIBLE_HOME", "ANSIBLE_LOCAL_TEMP", "ANSIBLE_SSH_CONTROL_PATH_DIR"):
            self.assertTrue(Path(envvars[name]).is_relative_to(private), name)
        self.assertEqual(envvars["ANSIBLE_HOST_KEY_CHECKING"], "True")

    def test_ansible_reads_the_run_s_own_configuration_and_searches_no_project_path_but_its_roles(self):
        from hq_runner.ansible import PLUGIN_PATHS, arguments

        private = self.root / "run-example"

        found = arguments(self.bound_run(), private)
        envvars = found["envvars"]

        config = Path(envvars["ANSIBLE_CONFIG"])
        self.assertTrue(config.is_file())
        self.assertTrue(config.is_relative_to(private))
        for name in PLUGIN_PATHS:
            self.assertTrue(Path(envvars[name]).is_relative_to(private), name)
            self.assertNotIn(":", envvars[name], name)
        self.assertEqual(envvars["ANSIBLE_ROLES_PATH"], str(self.root / "project" / "roles"))

    def test_ansible_runner_ends_a_run_at_its_procedure_s_timeout(self):
        from hq_runner.ansible import arguments

        self.assertEqual(arguments(self.bound_run(), self.root / "run-example")["timeout"], 60)


class DocumentTests(DocumentTestCase):
    def test_only_runner_role_connections_reach_a_machine(self):
        self.write(
            fakes.connection("app.example.com", role="runner"),
            fakes.connection("edge.example.com"),
            fakes.connection("caddy.example.com", role="caddy"),
            {
                "ref": "dns",
                "provider": "cloudflare",
                "manages": False,
                "store": {"vault": "Example", "item": "dns"},
                "api_token": {"api_token": "example-token"},
            },
        )

        self.assertEqual(set(self.targets()), {"app.example.com"})

    def test_a_ref_two_connections_share_reaches_nothing(self):
        self.write(
            fakes.connection("app.example.com", role="runner", host="192.0.2.10"),
            fakes.connection("app.example.com", role="runner", host="192.0.2.11"),
        )

        self.assertEqual(self.targets(), {})

    def test_a_connection_that_is_not_a_usable_destination_reaches_nothing(self):
        self.write(
            fakes.connection("a.example.com", role="runner", host="-oProxyCommand=true"),
            fakes.connection("b.example.com", role="runner", port="0"),
            fakes.connection("c.example.com", role="runner", user="root;true"),
            fakes.connection("../escape", role="runner"),
        )

        self.assertEqual(self.targets(), {})

    def test_a_document_another_account_can_read_or_of_another_version_is_refused(self):
        from hq_runner.connections import ConnectionsError

        for options in ({"mode": 0o644}, {"schema_version": 1}):
            with self.subTest(**options):
                self.write(fakes.connection("app.example.com", role="runner"), **options)
                with self.assertRaises(ConnectionsError):
                    self.targets()

    def test_a_document_that_is_a_link_is_refused(self):
        from hq_runner.connections import ConnectionsError

        real = self.write(fakes.connection("app.example.com", role="runner"))
        linked = self.root / "linked.json"
        linked.symlink_to(real)

        from hq_runner.connections import runner_targets

        with self.assertRaises(ConnectionsError):
            runner_targets(linked, self.ssh_dir)
