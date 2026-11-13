"""One scan and one run through the worker's own entry point, the real
ansible-runner, ansible-doc and ansible-playbook, on this machine alone, over
the fake bridge: what ``mise run smoke:runner`` runs, in an environment of the
runner group alone, so it also shows the worker needs nothing of HQ's own
dependencies.

The repository is the example project (``fakes.EXAMPLE_PROJECT``), committed to
one on this machine (``remote``): Ansible's sample layout, with an
``ansible.cfg`` that would enable callbacks of its own, from
``callback_plugins/`` and from a collection in ``collections/``, and send roles
elsewhere, were it read. The run passes only if none of it loads, and its machine is reached through a runner connection
in a connections document as the renderer writes one; the playbook alone sets
its connection to local, so no ssh is opened.

It also holds the worker's copies of Ansible's own names to the ansible-core
installed: the plugin paths it pins, the plugin directories a scan reports,
and the role specs the tests' fake ansible-doc answers.
"""

import dataclasses
import os
import sys
import tempfile
from pathlib import Path

from hq_runner import ansible, catalog
from hq_runner.__main__ import Config, drain

from . import fakes
from .remote import REPOSITORY, Remote
from .test_catalog import project_files

WANTED = [("Say which service", "ok"), ("Change something", "changed")]
# A read that asks to escalate in every way a play can, beside the example
# project's procedures: through the read login, each task runs as the login
# itself, so it ends well only while the worker's extra var outranks them all.
ESCALATING = "playbooks/escalating_read.yml"
# Each task checks it ran as the login itself, as the role's does.
_AS_ITSELF = """\
- name: Run as whoever the task runs as
  ansible.builtin.command: id -u
  become: true
  become_method: ansible.builtin.su
  vars:
    ansible_become: true
  register: ran
  changed_when: false
- name: The read ran as its own login
  ansible.builtin.assert:
    that: ran.stdout == lookup('ansible.builtin.pipe', 'id -u')
"""
ESCALATING_FILES = {
    ESCALATING: """\
- name: Ask to escalate from the read login
  hosts: all
  gather_facts: false
  become: true
  become_user: root
  vars:
    ansible_connection: local
    ansible_become: true
  roles:
    - escalating
  tasks:
    - name: Set escalation as a fact
      ansible.builtin.set_fact:
        ansible_become: true
    - name: Read escalation from a file
      ansible.builtin.include_vars: escalating_vars.yml
    - name: Escalate a block
      become: true
      block:
        - name: Check from inside it
          ansible.builtin.include_tasks: escalating_tasks.yml
""",
    "playbooks/escalating_vars.yml": "ansible_become: true\n",
    "playbooks/escalating_tasks.yml": _AS_ITSELF,
    "roles/escalating/tasks/main.yml": _AS_ITSELF,
}


def _environment(root: Path) -> dict[str, str]:
    connections = root / "connections.json"
    fakes.write_connections(connections, fakes.runner("localhost", host="127.0.0.1"))
    for directory in ("scratch", "state", "ssh"):
        (root / directory).mkdir()
    return {
        "HQ_AUTOMATION_ID": fakes.WORKER,
        "HQ_AUTOMATION_BRIDGE_SOCKET": str(root / "bridge" / "bridge.sock"),
        "HQ_AUTOMATION_JOURNAL": str(root / "state" / "journal.json"),
        "HQ_AUTOMATION_CONNECTIONS": str(connections),
        "HQ_AUTOMATION_SSH_DIR": str(root / "ssh"),
        "HQ_AUTOMATION_SCRATCH": str(root / "scratch"),
        "HQ_AUTOMATION_REPOSITORY": REPOSITORY,
    }


def _ansible_names() -> list[str]:
    """Where the worker's copies of Ansible's names differ from ansible-core's."""

    from ansible.config.manager import ConfigManager
    from ansible.plugins.loader import get_all_plugin_loaders

    definitions = ConfigManager().get_configuration_definitions()
    searched = {
        entry["name"]
        for name, definition in definitions.items()
        if name.endswith("_PLUGIN_PATH") or name in ("DEFAULT_MODULE_PATH", "DEFAULT_MODULE_UTILS_PATH")
        for entry in definition.get("env", ())
    }
    subdirs = {loader.subdir for _, loader in get_all_plugin_loaders() if loader.subdir}
    wrong = []
    if set(ansible.PLUGIN_PATHS) != searched:
        wrong.append(f"ansible.PLUGIN_PATHS is not ansible-core's plugin paths: {sorted(searched)}")
    if catalog.PLUGIN_DIRECTORIES - {"collections", "plugins"} != subdirs:
        wrong.append(f"catalog.PLUGIN_DIRECTORIES is not ansible-core's plugin directories: {sorted(subdirs)}")
    return wrong


def main() -> int:
    wrong = _ansible_names()
    with tempfile.TemporaryDirectory(prefix="hq-runner-") as directory:
        root = Path(directory)
        remote = Remote(root / "remote")
        revision = remote.commit({**project_files(), **ESCALATING_FILES})
        run, escalating = (fakes.pending(revision=revision, effect="read", privilege="") for _ in range(2))
        for asked, playbook in ((run, "playbooks/host_health.yml"), (escalating, ESCALATING)):
            asked["resource"]["spec"]["playbook"] = playbook
            asked["input"]["hosts"] = ["localhost"]
        escalating["operation"]["id"] = "33333333-4444-4555-8666-777777777777"
        bridge = fakes.Bridge(fakes.scan_pending(), run, escalating)
        config = Config.read(_environment(root))
        # The one change from the configuration the environment gives: the
        # repository is this machine's, reached over git's file transport.
        config = dataclasses.replace(config, project=dataclasses.replace(config.project, origin=remote.origin))
        loaded = root / "loaded"
        os.environ["HQ_EXAMPLE_LOADED"] = str(loaded)
        worked = drain(config, bridge)
        left = sorted(path.name for path in (root / "scratch").iterdir())
        if loaded.exists():
            wrong.append("the project's own ansible.cfg loaded a callback of the project's")
    scanned, ran, read = [*bridge.reports, {}, {}, {}][:3]
    specs = {
        found["role"]: found["argument_spec"]
        for procedure in scanned.get("catalog", {}).get("procedures", [])
        for found in procedure["roles"]
    }
    recorded = {role: ansible.entry_point(role, {role: spec}) for role, spec in fakes.RECORDED_ROLE_SPECS.items()}
    seen = [(event["task"], event["outcome"]) for event in ran.get("events", [])]
    print(f"smoke:runner: {worked} operations; scan {scanned.get('message')}; run {ran.get('message')}, events {seen}")
    checked = [event["outcome"] for event in read.get("events", []) if event["task"] == "The read ran as its own login"]
    # One check in the role, one in the escalated block.
    if read.get("success") is not True or checked != ["ok", "ok"]:
        wrong.append(f"a read that asks to escalate did not run as its own login: {read.get('message')}, {checked}")
    if scanned.get("catalog", {}).get("reaches") != [{"machine": "localhost", "profile": "read"}]:
        wrong.append(
            f"the scan reported the logins {scanned.get('catalog', {}).get('reaches')}, not the one its connections hold"
        )
    if worked != 3 or scanned.get("success") is not True:
        wrong.append(f"wanted a scan that succeeded first, not {scanned}")
    elif specs != recorded:
        wrong.append(f"ansible-doc answered {specs}, not the role specs recorded_role_specs.json holds")
    if ran.get("success") is not True or [step for step in seen if step in WANTED] != WANTED:
        wrong.append(f"wanted a run that succeeded with events {WANTED}")
    if left:
        wrong.append(f"the passes left {left} behind in their scratch directory")
    for each in wrong:
        print(f"smoke:runner: {each}", file=sys.stderr)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
