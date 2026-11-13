"""A scan reads the tip of the repository's default branch and reports each
procedure its sidecars declare, as Ansible's own files say it, every file it
holds and every path Ansible would read beyond a playbook and its roles,
without running anything of it.

The repository is the example project (``fakes.EXAMPLE_PROJECT``), committed to
one on this machine (``remote``). Its catalog is ``recorded_scan.json``, which
HQ's side reads to show a Refresh stores what the worker found; after a change
to the project, ``HQ_RUNNER_RECORD_SCAN=1 mise run test hq_runner.tests.test_catalog``
writes it again.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from hq_runner import catalog, procedures
from hq_runner.claim import Refused

from . import fakes
from .remote import REPOSITORY, Remote

RECORDED = Path(__file__).with_name("recorded_scan.json")


def project_files(root: Path = fakes.EXAMPLE_PROJECT) -> dict[str, str]:
    """The example project's files, by their path inside it."""

    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.remote = Remote(self.root / "remote")
        (self.root / "scratch").mkdir()
        self.project = procedures.Project(REPOSITORY, origin=self.remote.origin)

    def scan(self, project: procedures.Project | None = None) -> dict:
        return catalog.scan(
            project or self.project,
            scratch=self.root / "scratch",
            token="",
            role_spec=fakes.role_spec,
            verify=fakes.verified,
            reaches=lambda: fakes.REACHES,
        )

    def by_sidecar(self, scanned: dict) -> dict[str, dict]:
        return {found["sidecar"]: found for found in scanned["procedures"]}


class ExampleProjectTests(ScanTestCase):
    def setUp(self):
        super().setUp()
        self.revision = self.remote.commit(project_files())
        self.scanned = self.scan()

    def test_the_catalog_is_the_one_recorded_for_the_example_project(self):
        if os.environ.get("HQ_RUNNER_RECORD_SCAN"):
            RECORDED.write_text(json.dumps(self.scanned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.assertEqual(self.scanned, json.loads(RECORDED.read_text(encoding="utf-8")))

    def test_it_reads_the_tip_of_the_default_branch(self):
        self.assertEqual(self.scanned["branch"], "main")
        self.assertEqual(self.scanned["revision"], self.revision)
        self.assertEqual(self.scanned["verification"], {"verified": True, "reason": "valid", "committer": "web-flow"})

    def test_each_sidecar_is_a_procedure_read_from_the_playbook_beside_it_and_its_roles(self):
        procedures_found = self.by_sidecar(self.scanned)

        self.assertEqual(
            set(procedures_found),
            {"playbooks/host_health.hq.yml", "playbooks/service_restart.hq.yml", "playbooks/unfinished.hq.yml"},
        )
        health = procedures_found["playbooks/host_health.hq.yml"]
        self.assertEqual(health["playbook"], "playbooks/host_health.yml")
        self.assertEqual(health["declaration"]["id"], "example.host_health")
        self.assertEqual(health["title"], "Check a service on the host")
        self.assertEqual(health["preconditions"], ["The service to check is named"])
        self.assertEqual(health["verification"], ["The check ran"])
        ((role, spec),) = [(found["role"], found["argument_spec"]) for found in health["roles"]]
        self.assertEqual(role, "health")
        self.assertEqual(spec["options"]["service"]["choices"], ["web", "db"])
        self.assertEqual(health["problems"], [])

    def test_a_sidecar_with_no_playbook_beside_it_is_reported_with_that_problem(self):
        unfinished = self.by_sidecar(self.scanned)["playbooks/unfinished.hq.yml"]

        self.assertEqual(unfinished["playbook"], "")
        self.assertEqual(unfinished["declaration"]["effect"], "sometimes")
        self.assertTrue(any("playbooks/unfinished.yml" in problem for problem in unfinished["problems"]))

    def test_a_configuration_plugins_and_shared_variables_are_reported_wherever_they_sit(self):
        self.assertEqual(
            {(found["path"], found["kind"]) for found in self.scanned["unpinned"]},
            {
                ("ansible.cfg", "config"),
                ("callback_plugins", "plugins"),
                ("collections", "plugins"),
                ("roles/service/library", "plugins"),
                ("group_vars", "variables"),
            },
        )

    def test_every_file_is_listed_with_the_object_that_names_its_content(self):
        files = {found["path"]: found["object"] for found in self.scanned["files"]}

        self.assertEqual(set(files), set(project_files()))
        self.assertEqual(
            files["playbooks/host_health.yml"], self.remote.git("rev-parse", "HEAD:playbooks/host_health.yml")
        )


class UnreadableTests(ScanTestCase):
    def test_a_sidecar_that_links_out_of_the_checkout_is_a_problem_and_nothing_is_read_through_it(self):
        outside = self.root / "outside.yml"
        outside.write_text("id: example.leaked\n", encoding="utf-8")
        self.remote.commit(
            {"playbooks/leak.yml": "- hosts: all\n  name: Leak\n"}, links={"playbooks/leak.hq.yml": str(outside)}
        )

        (leak,) = self.scan()["procedures"]

        self.assertIsNone(leak["declaration"])
        self.assertTrue(leak["problems"])

    def test_a_role_named_by_anything_but_a_name_is_never_handed_to_ansible_doc(self):
        asked: list[str] = []

        def role_spec(role, playbook_dir, project_dir):
            asked.append(role)
            return fakes.role_spec(role, playbook_dir, project_dir)

        play = "- name: Odd\n  hosts: all\n  roles:\n    - --version\n    - ../elsewhere\n    - health\n"
        self.remote.commit({"playbooks/odd.yml": play, "playbooks/odd.hq.yml": "id: example.odd\n"})

        (odd,) = catalog.scan(
            self.project,
            scratch=self.root / "scratch",
            token="",
            role_spec=role_spec,
            verify=fakes.verified,
            reaches=lambda: fakes.REACHES,
        )["procedures"]

        self.assertEqual(asked, ["health"])
        self.assertEqual(len(odd["problems"]), 2)

    def test_a_role_ansible_doc_cannot_find_is_a_problem_of_its_procedure(self):
        play = "- name: Missing\n  hosts: all\n  roles: [absent]\n"
        self.remote.commit({"playbooks/missing.yml": play, "playbooks/missing.hq.yml": "id: example.missing\n"})

        (missing,) = self.scan()["procedures"]

        self.assertEqual(missing["roles"], [{"role": "absent", "argument_spec": None}])
        self.assertTrue(any("absent" in problem for problem in missing["problems"]))

    def test_a_value_ansible_tags_is_read_as_the_text_under_its_tag(self):
        play = "- name: !unsafe 'Tagged {{ title }}'\n  hosts: all\n  vars:\n    secret: !vault |\n      $ANSIBLE_VAULT;1.1;AES256\n      6162\n"
        self.remote.commit({"playbooks/tagged.yml": play, "playbooks/tagged.hq.yml": "id: example.tagged\n"})

        (tagged,) = self.scan()["procedures"]

        self.assertEqual(tagged["title"], "Tagged {{ title }}")
        self.assertEqual(tagged["problems"], [])

    def test_a_repository_with_no_default_branch_is_refused(self):
        with self.assertRaises(Refused) as refused:
            self.scan()

        self.assertEqual(refused.exception.reason, "FetchFailed")


READ = "id: example.check\neffect: read\n"
CHANGE = "id: example.check\neffect: infrastructure_change\nprivilege: example.service_control\n"


class EscalationTests(ScanTestCase):
    """A read whose plays escalate anywhere is a problem of that procedure,
    naming the file that does; a change may escalate."""

    def problems(self, sidecar: str, files: dict[str, str]) -> list[str]:
        self.remote.commit({"playbooks/check.hq.yml": sidecar, **files})
        (found,) = catalog.scan(
            self.project,
            scratch=self.root / "scratch",
            token="",
            role_spec=lambda role, playbook_dir, project_dir: None,
            verify=fakes.verified,
            reaches=lambda: fakes.REACHES,
        )["procedures"]
        return found["problems"]

    def test_a_read_that_escalates_anywhere_its_plays_reach_is_a_problem_naming_the_file(self):
        task = "- name: Look\n  ansible.builtin.command: id -u\n"
        cases = {
            "the play": ("playbooks/check.yml", {}, "  become: true\n  tasks: []\n"),
            "a task's vars": (
                "playbooks/check.yml",
                {},
                "  tasks:\n    - name: Look\n      vars:\n        ansible_become: true\n      ansible.builtin.command: id -u\n",
            ),
            "a file it imports": (
                "playbooks/tasks/look.yml",
                {"playbooks/tasks/look.yml": f"{task}  become_user: root\n"},
                "  tasks:\n    - ansible.builtin.import_tasks: tasks/look.yml\n",
            ),
            "a role it lists": (
                "roles/look/tasks/main.yml",
                {"roles/look/tasks/main.yml": f"{task}  become: yes\n"},
                "  roles: [look]\n",
            ),
            "a role it includes": (
                "roles/look/tasks/main.yml",
                {"roles/look/tasks/main.yml": f"{task}  become_method: ansible.builtin.su\n"},
                "  tasks:\n    - ansible.builtin.include_role:\n        name: look\n",
            ),
            "a role a role depends on": (
                "roles/deep/defaults/main.yml",
                {
                    "roles/look/meta/main.yml": "dependencies:\n  - role: deep\n",
                    "roles/look/tasks/main.yml": task,
                    "roles/deep/defaults/main.yml": "ansible_become_user: root\n",
                },
                "  roles: [look]\n",
            ),
        }
        for name, (escalating, files, body) in cases.items():
            with self.subTest(name):
                play = f"- name: Check\n  hosts: all\n{body}"
                problems = self.problems(READ, {"playbooks/check.yml": play, **files})

                self.assertEqual(len(problems), 1, problems)
                self.assertIn(escalating, problems[0])

    def test_a_read_that_turns_escalation_off_and_a_change_that_escalates_are_no_problem(self):
        off = "- name: Check\n  hosts: all\n  become: false\n  vars:\n    ansible_become: false\n  tasks: []\n"
        on = "- name: Check\n  hosts: all\n  become: true\n  tasks: []\n"

        self.assertEqual(self.problems(READ, {"playbooks/check.yml": off}), [])
        self.assertEqual(self.problems(CHANGE, {"playbooks/check.yml": on}), [])

    def test_a_scan_says_each_machine_and_profile_the_worker_has_a_login_for(self):
        self.remote.commit({"playbooks/check.hq.yml": READ, "playbooks/check.yml": "- name: Check\n  hosts: all\n"})

        scanned = self.scan()

        self.assertEqual({(found["machine"], found["profile"]) for found in scanned["reaches"]}, set(fakes.REACHES))


class VerificationTests(unittest.TestCase):
    def test_github_s_answer_for_a_commit_says_whether_it_is_signed_and_who_committed_it(self):
        self.assertEqual(
            catalog.verification(fakes.GITHUB_COMMIT), {"verified": True, "reason": "valid", "committer": "web-flow"}
        )

    def test_an_unsigned_commit_by_no_github_account_is_unverified_with_no_committer(self):
        commit = json.loads(json.dumps(fakes.GITHUB_COMMIT))
        commit["commit"]["verification"].update(verified=False, reason="unsigned", signature=None, payload=None)
        commit["committer"] = None

        self.assertEqual(catalog.verification(commit), {"verified": False, "reason": "unsigned", "committer": ""})

    def test_an_answer_that_says_nothing_of_a_signature_is_refused(self):
        with self.assertRaises(Refused):
            catalog.verification({"sha": fakes.REVISION, "commit": {}})

    def test_a_repository_not_on_github_is_never_verified(self):
        verify = catalog.github_verify(procedures.Project(REPOSITORY, origin="file:///srv/example"), "")

        self.assertEqual(verify(fakes.REVISION), {"verified": False, "reason": "unavailable", "committer": ""})
