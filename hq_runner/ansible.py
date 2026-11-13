"""The runner that runs a bound run's playbook through ansible-runner.

Every argument is fixed here or comes from the bound run (``claim.bind``): the
playbook and project from the installed procedure, the inventory from the
run's hosts, the extra-vars from its parameters. Nothing is read from the
private data directory's ``env/`` (a fresh, empty one per run), so no file
there can add a command-line option, a variable or a callback, and none is
written there (``suppress_env_files``). The artifacts live in a private
directory under ``scratch`` (tmpfs in the worker's image), hold no event the
handler did not keep and no stdout, and are removed with it when the run ends.
"""

import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

lazy import ansible_runner

from .claim import Run


def _inventory(hosts: tuple[str, ...]) -> dict[str, Any]:
    return {"all": {"hosts": {host: {} for host in hosts}}}


def arguments(run: Run, private_data_dir: Path) -> dict[str, Any]:
    """What ansible-runner is called with for ``run``, callbacks aside."""

    return {
        "private_data_dir": str(private_data_dir),
        "artifact_dir": str(private_data_dir / "artifacts"),
        "ident": run.operation,
        "project_dir": str(run.procedure.project_dir),
        "playbook": run.procedure.playbook,
        "inventory": _inventory(run.hosts),
        "extravars": dict(run.parameters),
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
