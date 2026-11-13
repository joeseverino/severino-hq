"""The automation worker as a program: ``python -m hq_runner`` claims queued runs
and scans over the bridge, one pass at a time, until none is left, then exits.

Everything it reads is named by its environment, set by the host's launcher:
where the bridge, the journal, the connections document, the ssh directory and
its scratch space are, and which repository its procedures come from. Nothing
in a claim names any of them. Each pass works in a directory of its own under
the scratch space (the procedure's checkout, ansible-runner's private data),
removed when the pass ends; only the journal outlives it.
"""

import os
import socket
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from . import ansible, catalog, connections, procedures
from .bridge import Bridge, BridgeError
from .claim import Refused
from .journal import Journal
from .worker import Bridge as BridgeProtocol, Runner, Scan, work_once

# The variables the launcher sets: the host side calls this worker
# "automation", since "runner" there is the Actions runner's word.
ID = "HQ_AUTOMATION_ID"
BRIDGE_SOCKET = "HQ_AUTOMATION_BRIDGE_SOCKET"
JOURNAL = "HQ_AUTOMATION_JOURNAL"
CONNECTIONS = "HQ_AUTOMATION_CONNECTIONS"
SSH_DIR = "HQ_AUTOMATION_SSH_DIR"
SCRATCH = "HQ_AUTOMATION_SCRATCH"
REPOSITORY = "HQ_AUTOMATION_REPOSITORY"
TOKEN_FILE = "HQ_AUTOMATION_TOKEN_FILE"  # noqa: S105 a variable's name, not a token
# The longest token file read: a GitHub installation token is far shorter.
_TOKEN_LIMIT = 4096


class ConfigError(Exception):
    """The worker's environment does not say what it needs."""


@dataclass(frozen=True, slots=True)
class Config:
    controller_id: str
    bridge_socket: str
    journal: Path
    connections: Path
    ssh_dir: Path
    scratch: Path
    project: procedures.Project
    # The GitHub token the repository is fetched with; none for a public one.
    token_file: Path | None = None

    @classmethod
    def read(cls, environ: Mapping[str, str]) -> Config:
        def path(name: str) -> Path:
            value = environ.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is not set")
            if not Path(value).is_absolute() or os.path.normpath(value) != value:
                raise ConfigError(f"{name} is not an absolute, clean path")
            return Path(value)

        repository = environ.get(REPOSITORY, "").strip()
        if not repository:
            raise ConfigError(f"{REPOSITORY} is not set")
        return cls(
            controller_id=environ.get(ID, "").strip() or socket.gethostname(),
            bridge_socket=str(path(BRIDGE_SOCKET)),
            journal=path(JOURNAL),
            connections=path(CONNECTIONS),
            ssh_dir=path(SSH_DIR),
            scratch=path(SCRATCH),
            project=procedures.Project(repository),
            token_file=path(TOKEN_FILE) if environ.get(TOKEN_FILE, "").strip() else None,
        )


def _token(path: Path | None) -> str:
    """The token as it is now: root replaces it while the worker runs."""

    if path is None:
        return ""
    try:
        with path.open("rb") as handle:
            token = handle.read(_TOKEN_LIMIT + 1).decode().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{TOKEN_FILE} is not readable") from exc
    if not token or len(token) > _TOKEN_LIMIT or any(character.isspace() for character in token):
        raise ConfigError(f"{TOKEN_FILE} holds no token")
    return token


def _reach(config: Config) -> Callable[[str, str], connections.Target | None]:
    """A pass's connections, read once when its first host is looked up."""

    read = cache(lambda: connections.runner_targets(config.connections, config.ssh_dir))
    return lambda host, profile: read().get((host, profile))


def _scan(config: Config, scratch: Path, token: str) -> Scan:
    """A pass's scan of the configured repository, read through Ansible and
    GitHub's API as the worker reaches them."""

    return lambda: catalog.scan(
        config.project,
        scratch=scratch,
        token=token,
        role_spec=ansible.role_spec(scratch),
        verify=catalog.github_verify(config.project, token),
        reaches=lambda: _reaches(config),
    )


def _reaches(config: Config) -> list[connections.Reach]:
    """Each machine and profile the worker has a login for; none while its
    connections cannot be read, as a run would find them."""

    try:
        return list(connections.runner_targets(config.connections, config.ssh_dir))
    except connections.ConnectionsError:
        return []


def drain(
    config: Config,
    bridge: BridgeProtocol,
    *,
    runner: Callable[[Path], Runner] = ansible.runner,
    scan: Callable[[Config, Path, str], Scan] = _scan,
) -> int:
    """Work every queued run and scan, each in a pass of its own; answer how many."""

    journal = Journal(config.journal)
    worked = 0
    while True:
        token = _token(config.token_file)
        with tempfile.TemporaryDirectory(prefix="pass-", dir=config.scratch) as directory:
            scratch = Path(directory)
            outcome = work_once(
                bridge,
                controller_id=config.controller_id,
                installed=procedures.resolver(config.project, scratch, token=token),
                reach=_reach(config),
                journal=journal,
                runner=runner(scratch),
                scan=scan(config, scratch, token),
                secrets=(token,) if token else (),
            )
        if outcome is None:
            return worked
        worked += 1
        conditions = outcome.report.get("conditions") or [{}]
        ended = "succeeded" if outcome.report["success"] else f"ended {conditions[0].get('reason', 'Failed')}"
        print(f"hq_runner: operation {outcome.operation} {ended}", flush=True)


def main(environ: Mapping[str, str] = os.environ) -> int:
    try:
        config = Config.read(environ)
        worked = drain(config, Bridge(config.bridge_socket))
    except (ConfigError, BridgeError, Refused) as exc:
        print(f"hq_runner: {exc}", file=sys.stderr)
        return 1
    print(f"hq_runner: {worked} operations worked, none left", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
