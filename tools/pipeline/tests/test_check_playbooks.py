"""A repository of playbooks checks its own checkout with the scan and the
judgment HQ's adoption runs, so the check refuses what adoption would: a
change that hands off to a unit is verified by the read that settles it, and
refused once neither asserts anything after its work. Ansible's answers
(each role's argument spec, the modules it ships) are fixed here."""

import tempfile
from pathlib import Path
from typing import Any

from django.test import SimpleTestCase

from hq_runner.tests.remote import Remote
from tools.pipeline.check_playbooks import check, checkout

REVISION = {"type": "str", "description": "The commit to apply."}
SIDECARS = {
    "playbooks/apply.hq.yml": """\
id: example.apply
target_kinds: [machine]
effect: infrastructure_change
privilege: example.deploy
settled_by: example.last
timeout_seconds: 600
retry: safe
recovery: [Run it again for the same revision.]
inputs:
  revision: {pattern: "^[0-9a-f]{40}$", max_length: 40}
""",
    "playbooks/last.hq.yml": """\
id: example.last
target_kinds: [machine]
effect: read
timeout_seconds: 60
retry: safe
recovery: [Nothing to recover.]
inputs:
  revision: {pattern: "^[0-9a-f]{40}$", max_length: 40}
""",
}
APPLY = """\
- name: Apply a revision
  hosts: all
  pre_tasks:
    - name: Require the unit to load
      ansible.builtin.assert: {that: [true]}
  roles: [apply]
"""
LAST = """\
- name: Read the last apply
  hosts: all
  roles: [last]
  post_tasks:
    - name: Require every app proven at the revision
      ansible.builtin.assert: {that: [true]}
"""
ROLE = "- name: Ask the host\n  ansible.builtin.command: {argv: [/usr/bin/true]}\n"


def _spec(role: str, *_: Path) -> dict[str, Any]:
    # The read may be asked for no revision, in words of its own.
    taken = REVISION if role == "apply" else {**REVISION, "required": False, "default": "", "description": "Its pull."}
    return {"options": {"revision": {"required": True, **taken}}}


class CheckPlaybooksTests(SimpleTestCase):
    def refused(self, last: str) -> tuple[str, ...]:
        with tempfile.TemporaryDirectory() as directory:
            remote = Remote(Path(directory))
            remote.commit(
                {
                    **SIDECARS,
                    "playbooks/apply.yml": APPLY,
                    "playbooks/last.yml": last,
                    "roles/apply/tasks/main.yml": ROLE,
                    "roles/last/tasks/main.yml": ROLE,
                }
            )
            known = frozenset({"ansible.builtin.assert", "ansible.builtin.command"})
            return check(checkout(remote.path), role_spec=_spec, modules=lambda: known)

    def test_a_change_settled_by_a_read_that_asserts_is_adopted(self):
        self.assertEqual(self.refused(LAST), ())

    def test_a_change_settled_by_a_read_that_asserts_nothing_is_refused(self):
        silent = LAST.split("  post_tasks:", maxsplit=1)[0]

        (said,) = self.refused(silent)

        self.assertTrue(said.startswith("playbooks/apply.hq.yml: "), said)
