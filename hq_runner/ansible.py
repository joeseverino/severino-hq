"""The worker's two uses of ansible-runner: a bound run's playbook, and a role's
argument spec as ``ansible-doc`` reads it. Every argument is fixed here or
comes from the bound run (``claim.bind``), so no claim or project adds an
option, a configuration file or a login. How a run is confined:
docs/Architecture.md, "The automation worker".
"""

import json
import shlex
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

lazy import ansible_runner

from .claim import Refused, Run
from .connections import Target

# The ssh options every connection is opened with; the host's known_hosts is
# the one the renderer wrote.
_SSH_OPTIONS = (
    "GlobalKnownHostsFile=/dev/null",
    "StrictHostKeyChecking=yes",
    "IdentitiesOnly=yes",
    "IdentityAgent=none",
    "BatchMode=yes",
    "PasswordAuthentication=no",
    "KbdInteractiveAuthentication=no",
    "ForwardAgent=no",
)

# Each path Ansible searches for plugins and modules beyond the ones it ships
# and the image installs, by the variable that sets it: its configuration's
# ``*_PLUGIN_PATH``, ``DEFAULT_MODULE_PATH`` and ``DEFAULT_MODULE_UTILS_PATH``
# (ansible-core 2.22's ``config/base.yml``; the smoke holds this to them).
PLUGIN_PATHS = (
    "ANSIBLE_ACTION_PLUGINS",
    "ANSIBLE_BECOME_PLUGINS",
    "ANSIBLE_CACHE_PLUGINS",
    "ANSIBLE_CALLBACK_PLUGINS",
    "ANSIBLE_CLICONF_PLUGINS",
    "ANSIBLE_CONNECTION_PLUGINS",
    "ANSIBLE_DOC_FRAGMENT_PLUGINS",
    "ANSIBLE_FILTER_PLUGINS",
    "ANSIBLE_HTTPAPI_PLUGINS",
    "ANSIBLE_INVENTORY_PLUGINS",
    "ANSIBLE_LIBRARY",
    "ANSIBLE_LOOKUP_PLUGINS",
    "ANSIBLE_MODULE_UTILS",
    "ANSIBLE_NETCONF_PLUGINS",
    "ANSIBLE_STRATEGY_PLUGINS",
    "ANSIBLE_TERMINAL_PLUGINS",
    "ANSIBLE_TEST_PLUGINS",
    "ANSIBLE_VARS_PLUGINS",
)
# Where a project's roles are, by Ansible's directory layout: ``roles/`` at its
# root, beside ``playbooks/``.
ROLES = "roles"
# The variable that says whether a task escalates, above each keyword.
BECOME = "ansible_become"
# How long ansible-doc may take to read one role's argument spec.
DOC_TIMEOUT_SECONDS = 60


def _host_vars(target: Target) -> dict[str, Any]:
    options = [f"UserKnownHostsFile={target.known_hosts}", *_SSH_OPTIONS]
    found: dict[str, Any] = {
        "ansible_connection": "ssh",
        "ansible_host": target.host,
        "ansible_port": target.port,
        "ansible_user": target.user,
        "ansible_ssh_private_key_file": str(target.identity),
        "ansible_ssh_common_args": shlex.join(part for option in options for part in ("-o", option)),
        "ansible_host_key_checking": True,
    }
    if target.shell_type:
        found["ansible_shell_type"] = target.shell_type
    return found


def _inventory(hosts: tuple[str, ...], targets: tuple[Target, ...]) -> dict[str, Any]:
    return {"all": {"hosts": {host: _host_vars(target) for host, target in zip(hosts, targets, strict=True)}}}


def environment(private_data_dir: Path, project_dir: Path) -> dict[str, str]:
    """What every Ansible program the worker starts is told, for a project at
    ``project_dir``: its configuration and every path it searches, all inside
    ``private_data_dir`` but the project's own roles.

    Writes the empty configuration file it names, since Ansible goes on to the
    working directory's ``ansible.cfg`` when the one ``ANSIBLE_CONFIG`` names
    does not exist.
    """

    config = private_data_dir / "ansible.cfg"
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.write_text("# The worker's own: a project's ansible.cfg is never read.\n", encoding="utf-8")
    # A directory nothing creates: ansible-runner adds its own callback's
    # directory after the callback path, and nothing else is found.
    nothing = str(private_data_dir / "plugins")
    return {
        "ANSIBLE_CONFIG": str(config),
        "ANSIBLE_HOME": str(private_data_dir / "home"),
        "ANSIBLE_LOCAL_TEMP": str(private_data_dir / "tmp"),
        "ANSIBLE_SSH_CONTROL_PATH_DIR": str(private_data_dir / "cp"),
        "ANSIBLE_HOST_KEY_CHECKING": "True",
        "ANSIBLE_ROLES_PATH": str(project_dir / ROLES),
        **dict.fromkeys(PLUGIN_PATHS, nothing),
    }


def extravars(run: Run) -> dict[str, Any]:
    """The run's parameters, and for a run through the read login, escalation
    turned off.

    Turned off as an extra var, the one place Ansible ranks above everything
    a project can set: play, block and task ``become`` keywords, and
    ``ansible_become`` in a play's or task's vars, ``set_fact`` or
    ``include_vars``, each of which outranks an inventory variable. Written
    after the parameters, so none of them can turn it back on.
    """

    found = dict(run.parameters)
    if not run.escalates:
        found[BECOME] = False
    return found


def arguments(run: Run, private_data_dir: Path) -> dict[str, Any]:
    """What ansible-runner is called with for ``run``, callbacks aside."""

    return {
        "private_data_dir": str(private_data_dir),
        "artifact_dir": str(private_data_dir / "artifacts"),
        "ident": run.operation,
        "project_dir": str(run.procedure.project_dir),
        "playbook": run.procedure.playbook,
        "inventory": _inventory(run.hosts, run.targets),
        "extravars": extravars(run),
        "envvars": environment(private_data_dir, run.procedure.project_dir),
        # ansible-runner's own job timeout: past it, the run is killed and
        # ends as ``timeout``.
        "timeout": run.timeout_seconds,
        "suppress_env_files": True,
        "suppress_output_file": True,
        "suppress_ansible_output": True,
        "quiet": True,
    }


def runner(scratch: Path) -> Callable[..., str]:
    """A runner whose private data lives under ``scratch``."""

    def run_playbook(
        run: Run, *, on_event: Callable[[Mapping[str, Any]], None], should_stop: Callable[[], bool]
    ) -> str:
        with tempfile.TemporaryDirectory(prefix="run-", dir=scratch) as private:

            def event_handler(event: Mapping[str, Any]) -> bool:
                on_event(event)
                # False keeps the event out of the artifact directory: the
                # report is the only record of a run that leaves the worker.
                return False

            thread, started = ansible_runner.run_async(
                **arguments(run, Path(private)),
                event_handler=event_handler,
                # Asked every few seconds while the playbook runs; True kills it.
                cancel_callback=should_stop,
            )
            thread.join()
            return str(started.status)

    return run_playbook


# The argument spec of role ``role`` at its ``main`` entry point, as a playbook
# in ``playbook_dir`` of the project at ``project_dir`` finds it; None when
# the role declares none. It may refuse.
RoleSpec = Callable[[str, Path, Path], Mapping[str, Any] | None]


def role_spec(scratch: Path) -> RoleSpec:
    """A reader of role argument specs through ansible-runner's
    ``get_role_argspec``, each read in a directory of its own under ``scratch``."""

    def read(role: str, playbook_dir: Path, project_dir: Path) -> Mapping[str, Any] | None:
        with tempfile.TemporaryDirectory(prefix="doc-", dir=scratch) as private:
            response, _said = ansible_runner.get_role_argspec(
                role,
                playbook_dir=str(playbook_dir),
                private_data_dir=private,
                # ansible-doc runs here, so no project file is its working directory's.
                host_cwd=private,
                envvars=environment(Path(private), project_dir),
                suppress_env_files=True,
                quiet=True,
                timeout=DOC_TIMEOUT_SECONDS,
            )
        return entry_point(role, response)

    return read


def entry_point(role: str, response: Any) -> Mapping[str, Any] | None:
    """The ``main`` entry point of ``role`` in what ``ansible-doc -t role -j``
    answered, or None when the role declares no argument spec.

    ansible-doc answers an empty object for a role it cannot find, and says
    why only in warnings that name the worker's own paths, so those are not
    repeated.
    """

    if not isinstance(response, Mapping) or not response:
        raise Refused("NoArgumentSpec", f"Role {role} is missing. Add it at roles/{role}/ in the repository.")
    found = response.get(role)
    if not isinstance(found, Mapping):
        raise Refused("NoArgumentSpec", f"Ansible described another role in place of {role}. Refresh to try again.")
    points = found.get("entry_points")
    main = points.get("main") if isinstance(points, Mapping) else None
    if not isinstance(main, Mapping):
        return None
    # Its options and words alone: ansible-doc adds where the role is on this
    # machine, which is the worker's and no part of the spec.
    kept = {name: main[name] for name in ("short_description", "description", "options") if name in main}
    spec: dict[str, Any] = json.loads(json.dumps(kept))
    return spec
