"""A run's machines are reached only through the runner-role connection that
names each as the run's privilege profile, as the inventory hands it to
Ansible: its address, port and user, its own identity alone and its pinned
host key, with no agent, password or other key, and nothing of any other
connection in the document. A run through the read login never escalates.

The documents are written as the root renderer writes them: the shape
``controller/api/hq-connections.openapi.json`` declares, in this account's
own private file.
"""

import json
import shlex
import sys
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
            fakes.runner("app.example.com", host="192.0.2.10"),
            fakes.runner("edge.example.com", host="198.51.100.7", port="7722", user="example-ops"),
        )
        targets = self.targets()

        hosts = ("app.example.com", "edge.example.com")
        inventory = _inventory(hosts, tuple(targets[host, "read"] for host in hosts))

        found = inventory["all"]["hosts"]
        self.assertEqual(set(found), set(hosts))
        edge = found["edge.example.com"]
        self.assertEqual(edge["ansible_connection"], "ssh")
        self.assertEqual(edge["ansible_host"], "198.51.100.7")
        self.assertEqual(edge["ansible_port"], 7722)
        self.assertEqual(edge["ansible_user"], "example-ops")
        self.assertEqual(edge["ansible_ssh_private_key_file"], str(self.ssh_dir / "edge.example.com-read"))
        self.assertTrue(edge["ansible_host_key_checking"])
        self.assertNotIn("ansible_shell_type", edge)
        self.assertEqual(found["app.example.com"]["ansible_port"], 22)
        self.assertEqual(
            found["app.example.com"]["ansible_ssh_private_key_file"], str(self.ssh_dir / "app.example.com-read")
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

        self.write(fakes.runner("win.example.com", shell_type="powershell"))
        (target,) = self.targets().values()

        host = _inventory(("win.example.com",), (target,))["all"]["hosts"]["win.example.com"]

        self.assertEqual(host["ansible_shell_type"], "powershell")

    def bound_run(self, profile: str = "read", parameters: dict | None = None):
        from hq_runner.claim import Procedure, Run

        self.write(fakes.runner("app.example.com", profile))
        targets = tuple(self.targets().values())
        return Run(
            "11111111-2222-4333-8444-555555555555",
            2,
            "example.check",
            Procedure("0" * 40, self.root / "project", "playbooks/check.yml"),
            ("app.example.com",),
            targets,
            parameters or {},
            60,
            profile,
        )

    def test_a_run_through_the_read_login_has_escalation_off_above_all_it_plays(self):
        from hq_runner.ansible import arguments, extravars

        run = self.bound_run(parameters={"service": "web", "ansible_become": True})
        found = arguments(run, self.root / "run")

        # An extra var, which outranks every keyword and variable a playbook sets.
        self.assertEqual(extravars(run), {"service": "web", "ansible_become": False})
        for host in found["inventory"]["all"]["hosts"].values():
            self.assertNotIn("ansible_become", host)

    def test_a_run_through_a_privilege_s_login_leaves_escalation_to_its_playbook(self):
        from hq_runner.ansible import extravars

        self.assertEqual(extravars(self.bound_run("example.service_control", {"service": "web"})), {"service": "web"})

    def test_a_parameter_s_text_reaches_ansible_as_data_never_a_template(self):
        from hq_runner.ansible import arguments, write_extravars

        template = "{{ lookup('ansible.builtin.pipe', 'id') }}"
        run = self.bound_run(parameters={"service": template, "names": [template, 3], "keep": True})
        private = self.root / "run"
        private.mkdir()

        written = write_extravars(run, private)

        # The file ansible-runner is pointed at, which no other account reads.
        self.assertEqual(arguments(run, private)["extravars"], str(written))
        self.assertEqual(written.stat().st_mode & 0o777, 0o600)
        # Every text is tagged, at any depth, and nothing else is: the tags
        # aside, it is the JSON of the values sent. smoke:runner holds that
        # Ansible leaves a tagged text as it is.
        text = written.read_text(encoding="utf-8")
        self.assertEqual(text.count("!unsafe "), 2)
        self.assertEqual(text.count(f"!unsafe {json.dumps(template)}"), 2)
        self.assertEqual(
            json.loads(text.replace("!unsafe ", "")),
            {"service": template, "names": [template, 3], "keep": True, "ansible_become": False},
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

    def test_ansible_finds_collections_only_where_the_worker_s_environment_installs_them(self):
        """Not beside a project, in a home directory or on Python's path: a
        repository cannot bring a collection of its own that way."""

        from hq_runner.ansible import arguments

        envvars = arguments(self.bound_run(), self.root / "run-example")["envvars"]

        self.assertEqual(
            envvars.get("ANSIBLE_COLLECTIONS_PATH"), str(Path(sys.prefix, "share", "ansible", "collections"))
        )
        self.assertEqual(envvars.get("ANSIBLE_COLLECTIONS_SCAN_SYS_PATH"), "False")

    def test_ansible_runner_ends_a_run_at_its_procedure_s_timeout(self):
        from hq_runner.ansible import arguments

        self.assertEqual(arguments(self.bound_run(), self.root / "run-example")["timeout"], 60)


class DocumentTests(DocumentTestCase):
    def test_only_runner_role_connections_reach_a_machine(self):
        self.write(
            fakes.runner("app.example.com"),
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

        self.assertEqual(set(self.targets()), {("app.example.com", "read")})

    def test_a_machine_s_read_and_change_logins_are_two_connections(self):
        self.write(
            fakes.runner("app.example.com", user="example-read"),
            fakes.runner("app.example.com", "example.service_control", user="example-change"),
        )
        targets = self.targets()

        self.assertEqual(targets["app.example.com", "read"].user, "example-read")
        self.assertEqual(targets["app.example.com", "example.service_control"].user, "example-change")
        self.assertNotEqual(
            targets["app.example.com", "read"].identity, targets["app.example.com", "example.service_control"].identity
        )

    def test_a_runner_connection_that_names_no_machine_or_profile_reaches_nothing(self):
        self.write(
            fakes.connection("app.example.com", role="runner"),
            fakes.connection("db.example.com", role="runner", machine="db.example.com"),
            fakes.connection("edge.example.com", role="runner", machine="edge.example.com", profile="Read Only"),
        )

        self.assertEqual(self.targets(), {})

    def test_a_ref_or_a_machine_and_profile_two_connections_share_reaches_nothing(self):
        shared_ref = fakes.runner("db.example.com")
        self.write(
            fakes.connection("app-a", role="runner", machine="app.example.com", profile="read", host="192.0.2.10"),
            fakes.connection("app-b", role="runner", machine="app.example.com", profile="read", host="192.0.2.11"),
            shared_ref,
            {**shared_ref, "ssh_transport": {**shared_ref["ssh_transport"], "machine": "edge.example.com"}},
        )

        self.assertEqual(self.targets(), {})

    def test_a_connection_that_is_not_a_usable_destination_reaches_nothing(self):
        self.write(
            fakes.runner("a.example.com", host="-oProxyCommand=true"),
            fakes.runner("b.example.com", port="0"),
            fakes.runner("c.example.com", user="root;true"),
            fakes.connection("../escape", role="runner", machine="d.example.com", profile="read"),
        )

        self.assertEqual(self.targets(), {})

    def test_a_document_another_account_can_read_or_of_another_version_is_refused(self):
        from hq_runner.connections import ConnectionsError

        for options in ({"mode": 0o644}, {"schema_version": 1}):
            with self.subTest(**options):
                self.write(fakes.runner("app.example.com"), **options)
                with self.assertRaises(ConnectionsError):
                    self.targets()

    def test_a_document_that_is_a_link_is_refused(self):
        from hq_runner.connections import ConnectionsError

        real = self.write(fakes.runner("app.example.com"))
        linked = self.root / "linked.json"
        linked.symlink_to(real)

        from hq_runner.connections import runner_targets

        with self.assertRaises(ConnectionsError):
            runner_targets(linked, self.ssh_dir)
