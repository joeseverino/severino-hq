"""The runner that runs a bound run's playbook through ansible-runner.

Every argument is fixed here or comes from the bound run (``claim.bind``): the
playbook and project from the resolved procedure, the inventory from the
connection each of the run's hosts is reached through, the extra-vars from its
parameters. Nothing is read from the
private data directory's ``env/`` (a fresh, empty one per run), so no file
there can add a command-line option, a variable or a callback, and none is
written there (``suppress_env_files``). The artifacts live in a private
directory under ``scratch`` (tmpfs in the worker's image), hold no event the
handler did not keep and no stdout, and are removed with it when the run ends,
as are Ansible's own working files and ssh's control sockets, which a
read-only root has nowhere else to put.

Every host is reached over ssh as its connection says, with its identity
alone (no agent, no password, no other key), against its host key as the
renderer pinned it, and nothing else: ansible-runner turns host key checking
off unless told otherwise, so the inventory turns it on.
"""

import shlex
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

lazy import ansible_runner

from .claim import Run
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


def _working_files(private_data_dir: Path) -> dict[str, str]:
    """Where Ansible keeps its own files during the run: inside the run's private directory."""

    return {
        "ANSIBLE_HOME": str(private_data_dir / "home"),
        "ANSIBLE_LOCAL_TEMP": str(private_data_dir / "tmp"),
        "ANSIBLE_SSH_CONTROL_PATH_DIR": str(private_data_dir / "cp"),
        "ANSIBLE_HOST_KEY_CHECKING": "True",
    }


def arguments(run: Run, private_data_dir: Path) -> dict[str, Any]:
    """What ansible-runner is called with for ``run``, callbacks aside."""

    return {
        "private_data_dir": str(private_data_dir),
        "artifact_dir": str(private_data_dir / "artifacts"),
        "ident": run.operation,
        "project_dir": str(run.procedure.project_dir),
        "playbook": run.procedure.playbook,
        "inventory": _inventory(run.hosts, run.targets),
        "extravars": dict(run.parameters),
        "envvars": _working_files(private_data_dir),
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
