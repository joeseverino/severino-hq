"""One scan and its runs through the worker's own entry point, the real
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
its connection to local, so no ssh is opened. Two runs Ansible stops before
their first task hold ``recorded_stopped_events.json`` to what it says.

It also holds the worker's copies of Ansible's own names to the ansible-core
installed: the plugin paths it pins, the plugin directories a scan reports,
the task keywords a scan reads as no module, and the role specs and modules
the tests' fake ansible-doc answers; and the collections installed to the
ones ``collections.yml`` pins.
"""

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hq_runner import ansible, catalog, events
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


# A read given a template as a parameter. Ansible reads an extra var's text as
# a template and runs its lookups on the worker, so the run ends well, and
# leaves no marker, only while the worker hands each parameter over as data.
TEMPLATED = "playbooks/templated_input.yml"
TEMPLATED_FILES = {
    TEMPLATED: """\
- name: Take a parameter that holds a template
  hosts: all
  gather_facts: false
  vars:
    ansible_connection: local
  tasks:
    - name: The parameter is the text sent
      ansible.builtin.assert:
        that: said is ansible.builtin.search('lookup')
""",
}


# Two playbooks Ansible stops before their first task, which
# ``recorded_stopped_events.json`` was recorded from: each run's report says
# the error the recorded events give.
STOPPED_FILES = {
    "playbooks/broken_role.yml": """\
- name: Use a role whose tasks do not parse
  hosts: all
  gather_facts: false
  vars:
    ansible_connection: local
  roles:
    - broken
""",
    "roles/broken/tasks/main.yml": """\
- name: Say something
  ansible.builtin.debug:
    msg: [unclosed
""",
    "playbooks/missing_collection.yml": """\
- name: Use a module from a collection the worker lacks
  hosts: all
  gather_facts: false
  vars:
    ansible_connection: local
  tasks:
    - name: Restart the stack
      example.absent.compose_restart:
        project: /srv/example
""",
}
STOPPED = {"broken_role": "playbooks/broken_role.yml", "missing_collection": "playbooks/missing_collection.yml"}


# Three procedures the scan judges by the modules their tasks name: one from a
# collection the worker ships, which is no problem and runs; the one above,
# from a collection it does not ship; and one by a name with no collection.
SHIPPED = "playbooks/container_facts.yml"
MODULE_FILES = {
    SHIPPED: """\
- name: Say whether the worker runs in a container
  hosts: all
  gather_facts: false
  vars:
    ansible_connection: local
  tasks:
    - name: Read the container facts
      community.docker.current_container_facts:
    - name: State them
      ansible.builtin.set_stats:
        data:
          result: "{{ {inventory_hostname: 'in a container' if ansible_module_running_in_container else 'not in a container'} }}"
        per_host: false
  post_tasks:
    - name: The facts say whether it is in a container
      ansible.builtin.assert:
        that: ansible_module_running_in_container is boolean
""",
    "playbooks/container_facts.hq.yml": "id: example.container_facts\neffect: read\n",
    "playbooks/missing_collection.hq.yml": "id: example.missing_collection\neffect: read\n",
    "playbooks/unqualified_module.yml": """\
- name: Name a module by its short name
  hosts: all
  gather_facts: false
  collections: [community.docker]
  tasks:
    - name: Read the container facts
      current_container_facts:
""",
    "playbooks/unqualified_module.hq.yml": "id: example.unqualified_module\neffect: read\n",
}
# What the scan says of each, by its sidecar: the module each problem names.
REFUSED_MODULES = {
    "playbooks/container_facts.hq.yml": [],
    "playbooks/missing_collection.hq.yml": ["example.absent.compose_restart"],
    "playbooks/unqualified_module.hq.yml": ["current_container_facts"],
}


def _recorded_stop(name: str) -> str | None:
    """What a run's report says of the recorded run ``name`` Ansible stopped."""

    stopped = events.Stopped(fakes.PROCEDURES[fakes.PROCEDURE].project_dir)
    for event in fakes.STOPPED[name]:
        stopped.read(event)
    return stopped.message


def _templated(marker: Path) -> str:
    return "{{ lookup('ansible.builtin.pipe', 'touch " + str(marker) + "') }}"


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
    from ansible.playbook.handler import Handler
    from ansible.playbook.task import Task
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
    # What parsing.mod_args.ModuleArgsParser reads as no module.
    keywords = {*Task.fattributes, *Handler.fattributes, "local_action", "static"}
    if keywords != catalog.TASK_KEYWORDS:
        wrong.append(f"catalog.TASK_KEYWORDS is not ansible-core's task keywords: {sorted(keywords)}")
    return wrong


def _collections(root: Path) -> list[str]:
    """Where the collections installed, and the modules ansible-doc lists
    with them, differ from what collections.yml pins and the tests' fake
    ansible-doc answers."""

    galaxy = subprocess.run(
        [Path(sys.executable).with_name("ansible-galaxy"), "collection", "list", "--format", "json"],
        env={**os.environ, **ansible.environment(root / "galaxy", root / "galaxy")},
        capture_output=True,
        text=True,
        timeout=ansible.DOC_TIMEOUT_SECONDS,
        check=False,
    )
    try:
        listed = json.loads(galaxy.stdout)
    except ValueError:
        return [f"ansible-galaxy listed no collections: {galaxy.stderr.strip()}"]
    installed = {name: found["version"] for each in listed.values() for name, found in each.items()}
    wrong = []
    if installed != catalog.shipped():
        wrong.append(f"the collections installed are {installed}, not the ones collections.yml pins")
    (root / "doc").mkdir()
    if missing := sorted(fakes.MODULES - set(ansible.modules(root / "doc")())):
        wrong.append(f"ansible-doc lists no {missing}, which fakes.MODULES names")
    return wrong


def main() -> int:
    wrong = _ansible_names()
    with tempfile.TemporaryDirectory(prefix="hq-runner-") as directory:
        root = Path(directory)
        remote = Remote(root / "remote")
        wrong.extend(_collections(root))
        revision = remote.commit(
            {**project_files(), **ESCALATING_FILES, **TEMPLATED_FILES, **STOPPED_FILES, **MODULE_FILES}
        )
        run, escalating, templated, shipped, *stops = (
            fakes.pending(revision=revision, effect="read", privilege="") for _ in range(4 + len(STOPPED))
        )
        for index, (asked, playbook) in enumerate(
            (
                (run, "playbooks/host_health.yml"),
                (escalating, ESCALATING),
                (templated, TEMPLATED),
                (shipped, SHIPPED),
                *zip(stops, STOPPED.values(), strict=True),
            )
        ):
            asked["resource"]["spec"]["playbook"] = playbook
            asked["input"]["hosts"] = ["localhost"]
            if index:
                asked["operation"]["id"] = f"{index + 2:08d}-4444-4555-8666-777777777777"
        evaluated = root / "evaluated"
        templated["input"]["parameters"] = {"said": _templated(evaluated)}
        bridge = fakes.Bridge(fakes.scan_pending(), run, escalating, templated, shipped, *stops)
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
        if evaluated.exists():
            wrong.append("a parameter's template was evaluated on the worker")
    reports = 5 + len(STOPPED)
    scanned, ran, read, given, used, *stopped = [*bridge.reports, *({} for _ in range(reports))][:reports]
    for name, report in zip(STOPPED, stopped, strict=True):
        if report.get("success") is not False or report.get("message") != _recorded_stop(name):
            wrong.append(
                f"the run of {STOPPED[name]} said {report.get('message')!r}, not {_recorded_stop(name)!r},"
                " which recorded_stopped_events.json gives"
            )
    if used.get("success") is not True:
        wrong.append(f"a run of a shipped collection's module did not succeed: {used.get('message')}")
    judged = {found["sidecar"]: found["problems"] for found in scanned.get("catalog", {}).get("procedures", [])}
    for sidecar, modules in REFUSED_MODULES.items():
        problems = judged.get(sidecar)
        if (
            problems is None
            or len(problems) != len(modules)
            or any(m not in p for p, m in zip(problems, modules, strict=True))
        ):
            wrong.append(f"the scan said {problems} of {sidecar}, not a problem naming each of {modules}")
    if given.get("success") is not True:
        wrong.append(f"a run given a template as a parameter did not take it as text: {given.get('message')}")
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
    if worked != reports or scanned.get("success") is not True:
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
