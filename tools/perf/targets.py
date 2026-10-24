"""The servers a report measures, each started, signed in to and stopped by its own block.

`Scratch` is a checkout's `mise run dev:scratch`, seeded with a named data set
(hq/platform/core/seeds.py); `Image` is a built image, run as
image:verify:healthy runs it and seeded the same way. Each signs in with `manage.py dev_session`, the session
dev:scratch hands a developer, and stops everything it started however the
block ends: a server runs in a process group of its own, which goes whole.
"""

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from tools.perf import parse
lazy from hq.platform.core import seeds

READY = "/health/ready/"
USER = "operator"


def shown_under(seed: str) -> dict[str, str]:
    """The settings ``seed`` is shown under, from its one declaration."""

    return dict(seeds.get(seed).settings)


PROBE = Path(__file__).with_name("queries_probe.py")
MARKER = "perf-queries "
# A first boot migrates and seeds a new database; a later one finds it made.
FIRST_BOOT_S = 900
BOOT_S = 180
# A scratch server's worker stops in under a second; its reloader can hang on past it, holding
# nothing a kill loses, since the database it leaves is SQLite's WAL.
STOP_S = 5
COLD_STARTS = 3
# What the run that started this set for itself, which a server of another
# checkout must not inherit: the gate's database and settings, the task's
# arguments, the environment and the task mise ran.
_OURS = (
    "SEVERINO_DATABASE_PATH",
    "SEVERINO_HQ_PLUGINS",
    "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION",
    "DJANGO_DEBUG",
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    "DJANGO_SETTINGS_MODULE",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "IMAGE",
    "MISE_CONFIG_ROOT",
    "MISE_ORIGINAL_CWD",
    "MISE_PROJECT_ROOT",
)


class NotMeasured(Exception):
    """A server would not start, sign in or answer, so nothing it serves was measured."""


def clean_env(**extra: str) -> dict[str, str]:
    """This environment without what the run set for itself, plus ``extra``."""

    kept = {
        name: value
        for name, value in os.environ.items()
        if name not in _OURS and not name.startswith(("usage_", "MISE_TASK_"))
    }
    return kept | extra


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def readiness(url: str) -> int | None:
    """The status ``url`` answers its readiness probe with, or None while nothing answers."""

    try:
        with urllib.request.urlopen(url + READY, timeout=2) as response:
            return response.status
    except urllib.error.HTTPError as error:
        with error:
            return error.code
    except OSError:
        return None


def wait_ready(url: str, deadline_s: float, alive=lambda: True) -> float:
    """Seconds until ``url`` answers ready, polled every 50 ms.

    A 4xx is a refusal no wait changes, so it fails at once; a 503 is a server not ready yet."""

    started = time.monotonic()
    while time.monotonic() - started < deadline_s:
        status = readiness(url)
        if status == 200:
            return time.monotonic() - started
        if status is not None and 400 <= status < 500:
            raise NotMeasured(f"the server at {url} refused the readiness probe with {status}")
        if not alive():
            raise NotMeasured(f"the server at {url} exited before it was ready")
        time.sleep(0.05)
    raise NotMeasured(f"the server at {url} was not ready within {deadline_s:.0f}s")


def run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    done = subprocess.run(command, capture_output=True, text=True, env=env, cwd=cwd, check=False)
    if done.returncode:
        said = (done.stderr or done.stdout).strip().splitlines()[-20:]
        raise NotMeasured(f"{' '.join(command[:6])} exited {done.returncode}:\n" + "\n".join(said))
    return done.stdout


def probe_counts(output: str) -> dict[str, int | None]:
    for line in output.splitlines():
        if line.startswith(MARKER):
            return json.loads(line.removeprefix(MARKER))
    raise NotMeasured(f"the query probe printed no counts: {output.strip()[-400:]!r}")


# Where a checkout names the extensions its scratch server composes: its
# gitignored mise.local.toml, and the two variables that file sets.
LOCAL = "mise.local.toml"
_LOCAL_SET = ("HQ_LOCAL_PLUGINS", "HQ_LOCAL_PYTHONPATH")


def scratch_env(tree: Path, *, trusted: bool, alone: bool) -> dict[str, str]:
    """What a tree's scratch tasks run under: alone, mise reads no mise.local.toml there and the
    extension set it names is not inherited either, so the server composes the host alone."""

    env = clean_env(**({"MISE_TRUSTED_CONFIG_PATHS": str(tree)} if trusted else {}))
    if alone:
        env = {name: value for name, value in env.items() if name not in _LOCAL_SET}
        env["MISE_IGNORED_CONFIG_PATHS"] = str(tree / LOCAL)
    return env


class Scratch:
    """`mise run dev:scratch --seed <seed>` in ``tree``, on a port of its own."""

    def __init__(self, tree: Path, log: Path, seed: str, trusted: bool = False, alone: bool = False) -> None:
        self.tree, self.log, self.seed = tree, log, seed
        self.env = scratch_env(tree, trusted=trusted, alone=alone)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server: subprocess.Popen | None = None
        self.measured = "footprint of the serving process"

    def _mise(self, *args: str) -> list[str]:
        return ["mise", "-C", str(self.tree), "run", *args]

    def _start(self, *flags: str) -> None:
        with self.log.open("a", encoding="utf-8") as sink:
            self.server = subprocess.Popen(
                self._mise("dev:scratch", "--port", str(self.port), *flags),
                env=self.env,
                stdout=sink,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

    def _alive(self) -> bool:
        return self.server is not None and self.server.poll() is None

    def __enter__(self) -> Scratch:
        self.log.write_text("", encoding="utf-8")
        try:
            self._start("--seed", self.seed)
            wait_ready(self.url, FIRST_BOOT_S, self._alive)
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        """The server's whole process group: mise, uv, the reloader and its worker."""

        if self.server is None:
            return
        stop_group(self.server)
        self.server = None

    def _read(self, task: str, *args: str, env: dict[str, str] | None = None) -> str:
        """What ``task`` prints, run raw so its stdout is the command's own, unlabeled, whatever the
        ref's own task declares."""

        return run(self._mise("--raw", task, *args), env=env or self.env)

    def session(self) -> tuple[str, str]:
        return parse.cookie(self._read("dev:session"))

    def queries(self, pages: list[str]) -> dict[str, int | None]:
        env = self.env | {"PERF_PAGES": json.dumps(pages), "PERF_USER": USER}
        code = PROBE.read_text(encoding="utf-8")
        return probe_counts(self._read("dev:manage", "--", "shell", "--no-imports", "-c", code, env=env))

    def memory(self) -> int:
        """The serving processes' footprint: under the reloader, its worker alone."""

        listening = run(["lsof", "-nP", f"-iTCP:{self.port}", "-sTCP:LISTEN", "-t"]).split()
        parents = {pid: run(["ps", "-o", "ppid=", "-p", pid]).strip() for pid in listening}
        workers = [pid for pid, parent in parents.items() if parent in parents] or listening
        return parse.footprint_bytes(
            run(["footprint", "-f", "bytes", "--noCategories", *(f"-p{pid}" for pid in workers)])
        )

    def cold_start(self) -> float:
        """Milliseconds from starting again on the database made to ready."""

        self.stop()
        started = time.monotonic()
        self._start("--keep")
        wait_ready(self.url, BOOT_S, self._alive)
        return (time.monotonic() - started) * 1000


def stop_group(process: subprocess.Popen) -> None:
    """Terminate ``process`` and everything in its group, killing what outlasts STOP_S."""

    for sent in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, sent)
        deadline = time.monotonic() + STOP_S
        while time.monotonic() < deadline:
            # Reaped, the leader no longer holds its group open.
            process.poll()
            if not _group_alive(process.pid):
                return
            time.sleep(0.1)


def _group_alive(group: int) -> bool:
    # macOS can answer EPERM instead of ESRCH while a stopped server's group is exiting; either
    # way nothing is left there that this process can signal.
    try:
        os.killpg(group, 0)
    except ProcessLookupError, PermissionError:
        return False
    return True


class Image:
    """A built image, run as image:verify:healthy runs it, seeded with a named data set."""

    def __init__(self, image: str, log: Path, seed: str) -> None:
        self.image, self.log, self.seed = image, log, seed
        self.name = f"severino-hq-perf-{os.getpid()}-{int(time.time())}"
        self.url = ""
        self.started = False
        self.measured = "the container's memory"

    def _docker(self, *args: str) -> str:
        return run(["docker", *args])

    def _manage(self, *args: str, env: dict[str, str] | None = None) -> str:
        flags = [f"--env={name}={value}" for name, value in (env or {}).items()]
        return self._docker("exec", *flags, self.name, "python", "manage.py", *args)

    def _seed(self) -> None:
        """The set, owned by the operator, as dev:scratch --seed makes it."""

        password = {"DJANGO_SUPERUSER_PASSWORD": os.urandom(12).hex()}
        if "seed" in self._manage("help", "--commands").split():
            self._manage("seed", self.seed, "--owner", USER, env=password)
            return
        # An image older than the seed registry, measured as the base, holds the showcase alone.
        if self.seed != "showcase":
            raise NotMeasured(f"{self.image} is older than the seed registry, so it seeds no {self.seed}")
        self._manage(
            "createsuperuser", "--noinput", "--username", USER, "--email", "operator@example.invalid", env=password
        )
        self._manage("seed_demo")

    def _published(self) -> str:
        address = self._docker("port", self.name, "8000/tcp").split()[0]
        return f"http://{address}"

    def _gateway(self) -> str:
        """The address a published port's requests arrive from: Docker's bridge gateway."""

        return self._docker("network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}").strip()

    def __enter__(self) -> Image:
        try:
            self._docker(
                "run",
                "--detach",
                "--name",
                self.name,
                "--publish",
                "127.0.0.1::8000",
                "--env",
                "DJANGO_SECRET_KEY=perf-only-container-key-0123456789abcdef0123456789abcdef",
                "--env",
                "DJANGO_ALLOWED_HOSTS=127.0.0.1",
                "--env",
                "DJANGO_SESSION_COOKIE_SECURE=0",
                "--env",
                "DJANGO_CSRF_COOKIE_SECURE=0",
                "--env",
                "SEVERINO_LOG_LEVEL=WARNING",
                *(f"--env={name}={value}" for name, value in shown_under(self.seed).items()),
                # HQ refuses any caller outside its trusted networks, and a
                # published port's requests come from the bridge, not loopback.
                "--env",
                f"SEVERINO_TRUSTED_NETWORKS=127.0.0.0/8,{self._gateway()}/32",
                self.image,
            )
            self.started = True
            self.url = self._published()
            wait_ready(self.url, FIRST_BOOT_S)
            self._seed()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        if self.started:
            self.log.write_text(
                subprocess.run(["docker", "logs", self.name], capture_output=True, text=True, check=False).stdout,
                encoding="utf-8",
            )
            subprocess.run(["docker", "rm", "--force", self.name], capture_output=True, check=False)
            self.started = False

    def session(self) -> tuple[str, str]:
        # dev_session refuses without DEBUG; the session it writes is the
        # serving process's, which shares the database and the key.
        return parse.cookie(self._manage("dev_session", USER, env={"DJANGO_DEBUG": "1"}))

    def queries(self, pages: list[str]) -> dict[str, int | None]:
        code = PROBE.read_text(encoding="utf-8")
        env = {"PERF_PAGES": json.dumps(pages), "PERF_USER": USER}
        return probe_counts(self._manage("shell", "--no-imports", "-c", code, env=env))

    def memory(self) -> int:
        return parse.docker_memory_bytes(self._docker("stats", "--no-stream", "--format", "{{.MemUsage}}", self.name))

    def cold_start(self) -> float:
        """Milliseconds from starting the stopped container again to ready."""

        self._docker("stop", self.name)
        started = time.monotonic()
        self._docker("start", self.name)
        self.url = self._published()
        wait_ready(self.url, BOOT_S)
        return (time.monotonic() - started) * 1000
