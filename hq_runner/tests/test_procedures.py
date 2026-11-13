"""A procedure runs only as its pinned commit has it: the resolver fetches that
commit by its id, refuses anything that checks out as another tree, keeps the
playbook inside the checkout, and hands git its token through the
environment alone.

Each test fetches from a repository on this machine (``remote``) over git's
file transport, as the worker fetches from GitHub over https.
"""

import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from .remote import REPOSITORY, Remote

PLAY = "- hosts: all\n  gather_facts: false\n  tasks: []\n"


class ResolverTestCase(unittest.TestCase):
    def setUp(self):
        from hq_runner.procedures import Project

        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.remote = Remote(self.root / "remote")
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.project = Project(REPOSITORY, origin=self.remote.origin)

    def resolve(self, revision: str, playbook: str = "playbooks/check.yml", **options):
        from hq_runner.procedures import resolver

        return resolver(self.project, self.scratch, **options)("example.check", revision, playbook)

    def refused(self, revision: str, playbook: str = "playbooks/check.yml") -> str:
        from hq_runner.claim import Refused

        with self.assertRaises(Refused) as caught:
            self.resolve(revision, playbook)
        return caught.exception.reason


class FetchTests(ResolverTestCase):
    def test_the_procedure_is_the_pinned_commit_s_even_once_the_branch_has_moved_on(self):
        reviewed = self.remote.commit({"playbooks/check.yml": PLAY + "# reviewed\n"})
        self.remote.commit({"playbooks/check.yml": PLAY + "# not reviewed\n"})

        procedure = self.resolve(reviewed)

        self.assertEqual(procedure.revision, reviewed)
        playbook = procedure.project_dir / procedure.playbook
        self.assertIn("# reviewed", playbook.read_text(encoding="utf-8"))
        head = subprocess.run(
            ["git", "-C", str(procedure.project_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"},
        ).stdout.strip()
        self.assertEqual(head, reviewed)
        self.assertTrue(procedure.project_dir.is_relative_to(self.scratch.resolve()))

    def test_a_commit_the_repository_does_not_have_is_refused(self):
        self.remote.commit({"playbooks/check.yml": PLAY})

        self.assertEqual(self.refused("0123456789abcdef0123456789abcdef01234567"), "FetchFailed")

    def test_an_id_that_names_a_tag_rather_than_its_commit_is_refused_as_another_tree(self):
        self.remote.commit({"playbooks/check.yml": PLAY})
        tag = self.remote.tag("v1")

        self.assertEqual(self.refused(tag), "RevisionMismatch")

    def test_a_commit_without_the_playbook_is_refused(self):
        revision = self.remote.commit({"playbooks/other.yml": PLAY})

        self.assertEqual(self.refused(revision), "NotInstalled")

    def test_the_token_reaches_git_through_its_environment_and_never_its_command_line(self):
        revision = self.remote.commit({"playbooks/check.yml": PLAY})
        token = "ghs_example0123456789token"
        with unittest.mock.patch("hq_runner.procedures.subprocess.run", wraps=subprocess.run) as ran:
            self.resolve(revision, token=token)

        headers = []
        for call in ran.call_args_list:
            self.assertFalse(any(token in argument for argument in call.args[0]), call.args[0])
            environment = call.kwargs["env"]
            self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
            self.assertEqual(environment["GIT_CONFIG_SYSTEM"], "/dev/null")
            settings = {
                environment[f"GIT_CONFIG_KEY_{index}"]: environment[f"GIT_CONFIG_VALUE_{index}"]
                for index in range(int(environment["GIT_CONFIG_COUNT"]))
            }
            self.assertEqual(settings["core.hooksPath"], "/dev/null")
            self.assertEqual(settings["transfer.fsckObjects"], "true")
            self.assertEqual(settings["protocol.allow"], "never")
            headers.append(settings["http.extraHeader"])
        self.assertTrue(headers)
        self.assertTrue(all(header.startswith("Authorization: Basic ") for header in headers))

    def test_a_repository_reached_by_another_transport_is_refused(self):
        from hq_runner.procedures import Project

        revision = self.remote.commit({"playbooks/check.yml": PLAY})
        self.project = Project(REPOSITORY, origin="ssh://git@example.com")

        self.assertEqual(self.refused(revision), "Unrunnable")


class PlaybookPathTests(ResolverTestCase):
    def test_a_playbook_path_that_is_not_plain_relative_yml_is_refused_before_any_fetch(self):
        revision = self.remote.commit({"playbooks/check.yml": PLAY})
        for playbook in ("../check.yml", "/etc/check.yml", "playbooks/../check.yml", "check.yaml", "-check.yml", ""):
            with self.subTest(playbook=playbook), unittest.mock.patch("hq_runner.procedures.subprocess.run") as ran:
                self.assertEqual(self.refused(revision, playbook), "Unrunnable")
                ran.assert_not_called()

    def test_a_playbook_that_links_out_of_the_checkout_is_refused(self):
        outside = self.root / "outside.yml"
        outside.write_text(PLAY, encoding="utf-8")
        revision = self.remote.commit({}, links={"playbooks/check.yml": str(outside)})

        self.assertEqual(self.refused(revision), "Unrunnable")
