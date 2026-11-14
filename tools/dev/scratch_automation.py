"""The automation worker beside a development server: always beside `mise run
dev`, and beside `mise run dev:scratch --runner`.

The server serves the bridge on a socket of its own, as the image does, and
`watch` stands where the host's automation units stand: when HQ rings the
automation doorbell, or a floor's time after the last worker ended, it writes
the worker's connections from the server database's machines and starts the
real worker (`work`), which drains what is queued and exits.

Its security properties:

- The bridge is a Unix socket in a directory only this account can enter,
  answered only to this account; nothing new listens on a network address.
- The worker is given the `HQ_AUTOMATION_*` variables and nothing of HQ's own
  environment: no signing key, no database path.
- Its repository is `scratch_playbooks/`, committed to a repository beside
  the server's database and fetched over git's file transport, with no token,
  so no fetch leaves this machine, whatever the database adopted before.
- It is given a login to one machine alone, the one HQ's inventory says
  this server runs on (``runs_hq``), so a run is asked, and recorded, only
  on the machine it really runs on. A scratch server whose inventory has no
  such machine declares one in its own database, at this host's addresses,
  through the operator's create command; `dev` declares none in its real
  data and gives the worker no login. That login reaches 127.0.0.1 and
  every play there runs with connection local, so no run reaches another
  host.
- The change playbook writes one file, in the run's own temporary
  directory, which goes when the run ends.

What it cannot show: GitHub's word on a commit's signature, since the
repository is local, so `work` vouches for each commit it scans; the
renderer's copy of real runner connections and identities; and the egress
table and container confinement the host gives the worker.
"""

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

lazy from django.conf import settings

lazy from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND
lazy from hq.platform.application.capabilities import execute_capability
lazy from hq.platform.application.hq_self import own_addresses
lazy from hq.platform.application.infrastructure import suggest_key
lazy from hq.platform.application.machines import machine_catalog
lazy from hq.platform.application.refusal_status import refusal_of
lazy from hq.platform.application.security import cli_principal
lazy from hq_runner import __main__ as entry, ansible, catalog, connections as logins
lazy from hq_runner.bridge import Bridge, BridgeError
lazy from hq_runner.claim import Refused
lazy from hq_runner.tests.remote import Remote

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOKS = Path(__file__).with_name("scratch_playbooks")
WORKER = "dev-worker"
# The repository the worker names; its origin is beside the database.
REPOSITORY = "example/automation"
# The profiles each machine is reached as: the read login, and each privilege
# a scratch playbook names.
PROFILES = ("read",)
LOOPBACK = "127.0.0.1"
# A login's account, which a local connection never uses.
USER = "dev"
# As the host's timer: a doorbell missed still starts a worker this long after the last.
FLOOR_SECONDS = 15 * 60
POLL_SECONDS = 1.0
# The doorbell's name, as hq.platform.application.cadence.AUTOMATION_DOORBELL.
DOORBELL = "automation-doorbell"
# The machine a scratch server declares itself as, when its inventory has none at this host's addresses.
HERE = "scratch-host"
# The profile whose server declares it: dev:scratch's (tools/dev/dev.sh).
DECLARES = "scratch"
# The verification `work` reports for a commit of the local repository.
VOUCHED = {"verified": True, "reason": "valid", "committer": "dev"}


@dataclass(frozen=True, slots=True)
class State:
    """Where the worker keeps what it reads and writes, beside the server's database."""

    root: Path

    @classmethod
    def of(cls, database: str) -> State:
        return cls(Path(database).parent / "automation")

    @property
    def doorbell(self) -> Path:
        return self.root.parent / DOORBELL

    @property
    def connections(self) -> Path:
        return self.root / "connections.json"

    @property
    def remote(self) -> Path:
        return self.root / "remote"

    def origin(self) -> str:
        return self.remote.as_uri()

    def environment(self, socket: str) -> dict[str, str]:
        """The worker's environment, as the host's launcher gives it."""

        return {
            "HQ_AUTOMATION_ID": WORKER,
            "HQ_AUTOMATION_BRIDGE_SOCKET": socket,
            "HQ_AUTOMATION_JOURNAL": str(self.root / "state" / "journal.json"),
            "HQ_AUTOMATION_CONNECTIONS": str(self.connections),
            "HQ_AUTOMATION_SSH_DIR": str(self.root / "ssh"),
            "HQ_AUTOMATION_SCRATCH": str(self.root / "scratch"),
            "HQ_AUTOMATION_REPOSITORY": REPOSITORY,
        }

    def prepare(self) -> str:
        """Each directory the worker opens, and the playbooks committed anew; the commit's id."""

        for directory in ("state", "ssh", "scratch", "home"):
            (self.root / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.rmtree(self.remote, ignore_errors=True)
        return Remote(self.remote).commit(playbook_files())


def playbook_files() -> dict[str, str]:
    """The scratch playbooks, by their path inside the project."""

    return {
        path.relative_to(PLAYBOOKS).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(PLAYBOOKS.rglob("*"))
        if path.is_file()
    }


def connections(machines: Iterable[str]) -> dict[str, Any]:
    """A connections document as the renderer writes one: a runner login to
    each machine as each profile, every one reaching this machine."""

    return {
        "schema_version": 2,
        "connections": [
            {
                "ref": f"{machine}-{profile}",
                "ssh_transport": {
                    "host": LOOPBACK,
                    "port": "22",
                    "user": USER,
                    "role": "runner",
                    "machine": machine,
                    "profile": profile,
                },
            }
            for machine in sorted(set(machines))
            for profile in PROFILES
        ],
    }


def write_connections(path: Path, document: Mapping[str, Any]) -> None:
    """Replace ``path`` with ``document``, a private single-link file, as the worker reads one."""

    written = path.with_name(path.name + ".new")
    written.unlink(missing_ok=True)
    descriptor = os.open(written, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    written.replace(path)


def here(catalog: Iterable[Any]) -> list[str]:
    """The machine of ``catalog`` this server runs on, by its name, or none."""

    return [machine.name for machine in catalog if machine.runs_hq]


def declare_here(addresses: Iterable[str]) -> list[str]:
    """Declare the machine ``HERE`` at ``addresses``, this host's, as the
    operator's create form does; its name, or none when it has no address or
    the declaration is refused."""

    addresses = list(addresses)
    if not addresses:
        return []
    spec = {"name": HERE, "addresses": addresses}
    answer = execute_capability(
        "infrastructure.resource.create",
        {
            "key": suggest_key(MACHINE_KIND, spec),
            "kind": MACHINE_KIND,
            "spec": spec,
            "enabled": True,
            "idempotency_key": str(uuid.uuid4()),
        },
        principal=cli_principal(),
    )
    if answer.get("ok") is False:
        print(f"automation: {HERE} was not declared: {refusal_of(answer).message}")
        return []
    print(f"automation: declared {HERE} at {', '.join(addresses)}, the machine this server runs on")
    return [HERE]


def write_reaches(*, declares: bool = False) -> None:
    """The worker's connections to the machine this server runs on, declared
    first when ``declares`` and there is none; run inside `manage.py shell`."""

    state = State.of(settings.DATABASES["default"]["NAME"])
    machines = here(machine_catalog())
    if not machines and declares:
        machines = declare_here(own_addresses())
    if not machines:
        print("automation: no machine in HQ's inventory holds an address of this host, so the worker reaches none")
    write_connections(state.connections, connections(machines))


def server_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _exit(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def watch(server: int, runner_python: str) -> int:
    """Start the worker each time HQ asks, for as long as ``server`` runs."""

    # Stopped by a signal, it still stops its worker and removes the socket's directory.
    for stop in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(stop, _exit)
    state = State.of(os.environ["SEVERINO_DATABASE_PATH"])
    socket = os.environ["SEVERINO_BRIDGE_SOCKET"]
    revision = state.prepare()
    print(f"automation: the worker scans {REPOSITORY} at {revision[:12]}, a commit of {PLAYBOOKS}", flush=True)
    worker: subprocess.Popen[bytes] | None = None
    ended = time.monotonic()
    try:
        while server_alive(server):
            if worker is not None and worker.poll() is not None:
                worker, ended = None, time.monotonic()
            # A doorbell the seed rang waits for the server's bridge, as one
            # rung while the host's web process is down waits for it.
            if (
                worker is None
                and Path(socket).exists()
                and (state.doorbell.exists() or time.monotonic() - ended >= FLOOR_SECONDS)
            ):
                worker = start_worker(state, socket, runner_python)
            time.sleep(POLL_SECONDS)
    finally:
        if worker is not None:
            worker.terminate()
            worker.wait()
        shutil.rmtree(Path(socket).parent, ignore_errors=True)
    return 0


def start_worker(state: State, socket: str, runner_python: str) -> subprocess.Popen[bytes]:
    # As the host's unit: the doorbell goes before the worker claims, so one
    # rung while it works still stands when it ends.
    state.doorbell.unlink(missing_ok=True)
    declares = os.environ.get("HQ_DEV_PROFILE") == DECLARES
    reaches = f"from tools.dev.scratch_automation import write_reaches; write_reaches(declares={declares})"
    subprocess.run(
        [sys.executable, "manage.py", "shell", "-v", "0", "-c", reaches],
        cwd=ROOT,
        check=False,
    )
    environment = {
        **state.environment(socket),
        "PATH": f"{Path(runner_python).parent}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(state.root / "home"),
    }
    return subprocess.Popen(
        [runner_python, "-m", "tools.dev.scratch_automation", "work", state.origin()], cwd=ROOT, env=environment
    )


def work(origin: str) -> int:
    """The worker's own entry point, with its repository at ``origin`` and each commit vouched for."""

    def reaches(config: entry.Config) -> list[logins.Reach]:
        with contextlib.suppress(logins.ConnectionsError):
            return list(logins.runner_targets(config.connections, config.ssh_dir))
        return []

    def scan(config: entry.Config, scratch: Path, token: str) -> Callable[[], dict[str, Any]]:
        return lambda: catalog.scan(
            config.project,
            scratch=scratch,
            token=token,
            role_spec=ansible.role_spec(scratch),
            modules=ansible.modules(scratch),
            verify=lambda _revision: VOUCHED,
            reaches=lambda: reaches(config),
        )

    try:
        config = entry.Config.read(os.environ)
        config = replace(config, project=replace(config.project, origin=origin))
        worked = entry.drain(config, Bridge(config.bridge_socket), scan=scan)
    except (entry.ConfigError, BridgeError, Refused) as exc:
        print(f"hq_runner: {exc}", file=sys.stderr)
        return 1
    print(f"hq_runner: {worked} operations worked, none left", flush=True)
    return 0


def main(arguments: list[str]) -> int:
    match arguments:
        case ["watch", server, runner_python]:
            return watch(int(server), runner_python)
        case ["work", origin]:
            return work(origin)
    print("usage: scratch_automation watch <server pid> <runner python> | work <origin>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
