"""Where a gate a person types runs: on the second machine HQ_GATE_REMOTE
names or here, whichever has the tokens it takes free now, that machine first
(docs/Development.md, Gates on a second machine). `python -m
tools.gate.remote <tokens|all>`, which tools/lib/remote.sh runs, runs the task
mise is running there and exits with its status, or exits HERE for the task to
run here.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from hq.platform.core.tests.task import RefError, Usage, commit_of, git
from tools.dev import extension_set
from tools.gate import chain, fast, room
from tools.gate.room import GATE, LOCK, lock_file
from tools.gate.tree import working_tree
from tools.gate.turn import ALL, held_locks, inside
from tools.gate.verdict import OUTPUT, TREE_LINE, shows_all

ROOT = chain.ROOT
# Where it runs, as git names a repository over SSH: <host>:<HQ's checkout there>.
SETTING = "HQ_GATE_REMOTE"
# The status that has the task run here (EX_TEMPFAIL).
HERE = 75
# Flags whose run acts on this machine (its browser, tracked files, Docker), so it stays here.
STAYS = {"open", "docs", "linux", "image"}
# Arguments that name a commit: resolved here, since a branch there is not this one, and sent with the tree.
REFS = {"base", "at"}
# Tasks that compose the extension set: fast, whose change may call for the composed suite, and perf.
COMPOSING = {"fast", "perf"}
# What this run sets that changes what a gate does, handed to the run there.
CARRIED = (
    *("HQ_BROWSER_CHANNEL", "HQ_BROWSER_ENGINE", "HQ_BROWSER_FACE", "HQ_BROWSER_SHARD", "HQ_GATE_OUTPUT"),
    *("HQ_LOCAL_PLUGINS", "HQ_TEST_STALL_SECONDS", "HYPOTHESIS_PROFILE", "REQUIRE_COMPOSED", "TEST_PYTHONS"),
)
# Every other variable a gate reads, by why the run there reads its own.
LEFT = {
    "a gate sets it for what it runs, there as here": (
        "HQ_TEST_WEIGHED",
        *("HQ_EXTENSIONS_SAID", "HQ_FAST_SOURCE", "HQ_GATE_DETAIL", "HQ_GATE_HERE"),
        *("HQ_GATE_RERUN", "HQ_GATE_TREE", "HQ_GATE_VERDICTS", "HQ_LOOK_SHARE", "HQ_LOOK_SHARES", "HQ_MEASURED"),
        *("HQ_TEST_MARKS", "HQ_TEST_REPORTS", "HQ_TURN_SHARE", "MCP_PROBE_DEFER_BUILD"),
        *("MEMORY_AFTER", "MEMORY_PAGES", "MEMORY_SEED", "MEMORY_SESSION", "MEMORY_TIMED", "PERF_HOST_ALONE_BECAUSE"),
        *("PERF_PAGES", "PERF_USER", "PYTHONPATH", "SANDBOX_WRITES", "SEVERINO_DATABASE_PATH"),
        *("SEVERINO_HQ_PLUGINS", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON"),
    ),
    "remote-host.sh names it to the run there": ("HQ_GATE_SENT",),
    "mise, or mise.toml's env, sets it for the task that reads it": (
        *("HQ_DEV_ACCOUNT", "MISE_CONFIG_ROOT", "MISE_STATE_DIR", "MISE_TASK_LOCKS", "MISE_TASK_LOCK_TIMEOUT"),
        *("MISE_TASK_NAME", "PYTHON_VERSIONS"),
    ),
    "the machine's own: its shell, home and folders": (
        *("COLIMA_HOME", "HOME", "IFS", "PWD", "TMPDIR", "XDG_CACHE_HOME", "XDG_STATE_HOME"),
    ),
    # A run that composes sends this machine's extension checkout by path (send); the set's manifests are carried.
    "a path on this machine: each machine's own extension checkout and second machine": (
        *("HQ_GATE_REMOTE", "HQ_LOCAL_PYTHONPATH"),
    ),
    "read by a run that never goes there: check, the image, CI": (
        *("CHECK_RELEASE_BASE_REF", "CHECK_REMOTE_POLL", "CHECK_REMOTE_TIMEOUT", "CI", "DJANGO_STATIC_ROOT"),
        *("GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "GITHUB_STEP_SUMMARY", "IMAGE", "RUNNER_IMAGE"),
        *("SEVERINO_FAILURE_LOG_IDENTITY", "SEVERINO_HQ_DEPLOY_HOST"),
    ),
}
# One connection carries every step; no agent or display reaches a gate run.
SSH = [
    *("ssh", "-a", "-x", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ControlMaster=auto"),
    *("-o", "ControlPath=/tmp/hq-gate-%C", "-o", "ControlPersist=10m"),
    # A connection that drops ends the run in a minute, never left waiting on a machine that is gone.
    *("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"),
]
HOST_HALF = Path(__file__).with_name("remote-host.sh")
# What the host half runs to say that machine's room, as this machine reads its own.
ROOM = Path(__file__).with_name("room.py")
# How long the ask for it may take before the run is left here.
ASK = 20


def words(spec: dict, values: dict[str, str]) -> list[str]:
    """The arguments from which mise parses ``values`` again, by the task's
    usage spec (`mise tasks info --json`'s ``usage_spec.cmd``)."""

    def given(entry: dict) -> list[str] | None:
        value = values.get(entry["name"].replace("-", "_"))
        return None if value is None else shlex.split(value) if entry.get("var") else [value]

    said: list[str] = []
    for flag in spec["flags"]:
        if (each := given(flag)) is not None:
            name = f"--{flag['long'][0]}" if flag["long"] else f"-{flag['short'][0]}"
            said += [part for value in each for part in [name, value]] if "arg" in flag else [name] * (each == ["true"])
    rest = [value for arg in spec["args"] if (each := given(arg)) is not None for value in each]
    return [*said, "--", *rest] if rest else said


def done(command: list[str], root: Path = ROOT, **options) -> str:
    try:
        return subprocess.run(command, cwd=root, capture_output=True, check=True, **options).stdout.decode().strip()
    except subprocess.CalledProcessError as error:
        raise RefError(f"{shlex.join(command[:3])} failed: {error.stderr.decode().strip()}") from None


def gate_commit(root: Path, tree: str) -> str:
    """A commit of ``tree`` on HEAD, HEAD itself when it holds the tree, so the run there reads this history.
    It takes HEAD's date, so the same tree on the same HEAD is the same commit and is sent once."""

    head, head_tree, when = git("log", "-1", "--format=%H%n%T%n%cI", root=root)
    if head_tree == tree:
        return head
    who = {"NAME": "gate", "EMAIL": "gate@example.invalid", "DATE": when}
    env = os.environ | {f"GIT_{role}_{key}": value for role in ("AUTHOR", "COMMITTER") for key, value in who.items()}
    return done(
        ["git", "commit-tree", "--no-gpg-sign", "-p", head, "-m", "The tree a gate judges", tree], root, env=env
    )


def push(root: Path, remote: str, refspecs: list[str]) -> None:
    """Push ``refspecs`` to the checkout there. Gate refs to a machine of the operator's own publish nothing,
    so the repository's pre-push hook, which guards a push to its forge, is not run for them."""

    command = ["git", "push", "--quiet", "--force", "--no-verify", remote, *refspecs]
    done(command, root, env=os.environ | {"GIT_SSH_COMMAND": shlex.join(SSH)})


def host_half(host: str, step: str, *args: str) -> list[str]:
    return [*SSH, host, shlex.join(["sh", "-c", HOST_HALF.read_text(), "remote-host.sh", step, *args])]


def bring_back(archive: bytes, sent: Sent, there: Where, host: str, task: str) -> None:
    """Keep what the run there wrote: each verdict that names the tree sent, the logs, looks and perf runs, and
    the pass it recorded, the verdicts and logs naming this checkout where they named the worktree there."""

    tree = sent.tree
    with tempfile.TemporaryDirectory() as folder:
        back = Path(folder)
        with tarfile.open(fileobj=io.BytesIO(archive)) as kept:
            kept.extractall(back, filter="data")
        for written in (*back.glob("var/gate/*.txt"), *back.glob("var/gate/*.log")):
            written.write_bytes(here(written.read_bytes(), there.where, there.home))
        for verdict in sorted(back.glob("var/gate/*.txt")):
            if verdict.read_text().splitlines()[:1] != [TREE_LINE.format(tree)]:
                print(f"[{task}] {host}'s var/gate/{verdict.name} names another tree, so it is not kept", flush=True)
                verdict.unlink()
            elif (mine := ROOT / "var/gate" / verdict.name).is_file():
                mine.replace(mine.with_suffix(".prev.txt"))
        for part in ("var/gate", "var/look", "var/perf"):
            if (back / part).is_dir():
                shutil.copytree(back / part, ROOT / part, dirs_exist_ok=True)
        fast = back / "var/gate/fast.txt"
        if (back / "var/back" / chain.PASSES[1] / tree).is_file() and fast.is_file():
            chain.record_elsewhere(tree, fast.read_text(), host)


def here(text: bytes, where: str, home: str) -> bytes:
    """``text`` with the worktree there, written whole or from ``~``, named as this checkout."""

    for there in (where, where.replace(home, "~", 1)):
        text = text.replace(there.encode(), str(ROOT).encode())
    return text


def as_here(line: bytes, task: str, where: str, home: str) -> bytes:
    """A line of the run there as a run here prints it: naming this checkout, and the task's own prefix left
    to mise here, which adds it again."""

    return here(line, where, home).removeprefix(f"[{task}] ".encode())


class Sent(NamedTuple):
    """A tree sent there: its commit, the arguments that ask for the task again, the run's id there (what it
    staged and what it keeps for back), and the extension checkout there and commit a composed run
    composes, with its packages' paths in it, or none, and the files fast's fixes rewrote here first."""

    tree: str
    commit: str
    argv: list[str]
    id: str
    extension: tuple[str, str] | None
    packages: str
    fixed: list[str]


class Where(NamedTuple):
    """The worktree there a run took, and that machine's home, which the run's lines name."""

    where: str
    home: str


# How the host half says which worktree the run took, on the line before the run's own.
READY = re.compile(rb"^(/\S+) (/\S+)\n$")


def sender() -> str:
    """This checkout, as the machine there keys the worktrees it reuses for it: each tree it sends resets
    the one it takes, whose environment and caches stay warm."""

    return hashlib.sha256(f"{socket.gethostname()}\0{ROOT}".encode()).hexdigest()[:16]


def composes(task: str, values: dict[str, str]) -> bool:
    """Whether the run may compose the extension set: the composed suite, a composed look, fast, which runs the
    suite when a change reaches the extensions, perf, and a look that runs perf after it (--perf, or --base
    unless --no-perf)."""

    measures = values.get("perf") == "true" or (bool(values.get("base")) and values.get("perf") != "false")
    return task in COMPOSING or values.get("composed") == "true" or (task == "dev:look" and measures)


def send(task: str, host: str, checkout: str, values: dict[str, str]) -> Sent:
    """Send this checkout's tree, and the commits its arguments name, and make it ready there."""

    # fast reads its default base from the passes and branches here, which the machine there lacks.
    if task == "fast" and "base" not in values:
        values["base"] = chain.fast_base(None)[0]
    # fast's fixes write this checkout, so they run here and the tree they leave is the one sent.
    fixed = fast.fixed_before_sending(values["base"]) if task == "fast" else []
    tree = working_tree(ROOT)
    commit = gate_commit(ROOT, tree)
    # Refs named for the run, so runs sending one tree at once (the extension's, from several worktrees) never
    # race to create the same ref, and the first to take its commit never deletes another's.
    run = uuid.uuid4().hex
    refs = {f"refs/gate/{run}": commit}
    for name in sorted(REFS & values.keys()):
        values[name] = refs[f"refs/gate/{run}-{name}"] = commit_of(values[name])
    remote = f"{host}:{checkout}"
    push(ROOT, remote, [f"{sha}:{ref}" for ref, sha in refs.items()])
    # A run that composes composes this machine's extension checkout, sent as the tree is, from its clone beside
    # HQ's there; one that cannot be sent leaves the run here, never composing that machine's own. Where the
    # set this tree can load is a commit of the checkout's history (tools/dev/extension_set.py), that commit
    # is sent, so the run there composes it as it stands; where it is another worktree of the repository (its
    # branch named as this tree lands on), that worktree's tree is.
    extension, packages = None, ""
    if composes(task, values) and (paths := os.environ.get("HQ_LOCAL_PYTHONPATH", "")):
        top = Path(git("rev-parse", "--show-toplevel", root=Path(paths.split(":")[0]))[0])
        packages = ":".join(str(Path(path).resolve().relative_to(top)) for path in paths.split(":"))
        # The clone there is named as the repository is, which a worktree of it here is not.
        common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", root=top)[0])
        chosen = extension_set.for_tree(ROOT)
        stands = Path(dict(chosen.stood).get(str(top), top))
        picked = dict(chosen.at).get(str(top)) or gate_commit(stands, working_tree(stands))
        extension = (str(PurePosixPath(checkout).parent / common.parent.name), picked)
        push(top, f"{host}:{extension[0]}", [f"{extension[1]}:refs/gate/{run}"])
    argv = words(json.loads(done(["mise", "tasks", "info", "--json", task]))["usage_spec"]["cmd"], values)
    # A file the run reads that no commit holds (a probe under var/) goes with the tree.
    if extras := chain.answered("check-ignore", "--", *(word for word in argv if (ROOT / word).is_file())):
        staged = io.BytesIO()
        with tarfile.open(fileobj=staged, mode="w") as archive:
            for path in extras:
                archive.add(ROOT / path, arcname=path)
        done(host_half(host, "stage", run), input=staged.getvalue())
    # fast's chain: the trees passed here and CI's maps of the tests that ran each line, fetched here where gh
    # is signed in, so it selects as it would here.
    if task == "fast":
        print(f"[{task}] {chain.fetch_map()}", file=sys.stderr, flush=True)
    if folders := [str(folder) for folder in (chain.passes(), chain.maps()) if folder.is_dir()]:
        done(["rsync", "-a", "-e", shlex.join(SSH), *folders, f"{remote}/.git/severino/"])
    return Sent(tree, commit, argv, run, extension, packages, fixed)


def run_there(task: str, host: str, checkout: str, values: dict[str, str], took: Callable[[str], None]) -> int:
    sent = send(task, host, checkout, values)
    print(f"[{task}] runs on {host}, tree {sent.tree[:9]}", file=sys.stderr, flush=True)
    # The run there writes to no terminal, so it is told how this run shows a gate's output.
    carried = {OUTPUT: "all" if shows_all(sys.stdout) else "short"} | {
        name: value for name, value in os.environ.items() if name in CARRIED
    }
    handed = [f"{name}={value}" for name, value in carried.items()]
    composed = [*(sent.extension or ("-", "")), sent.packages]
    asked = [sent.id, sender(), checkout, sent.tree, sent.commit, *composed, "env", *handed, "mise", "run", task]
    # Its input closing is what stops the run there.
    stream = subprocess.Popen(
        host_half(host, "run", *asked, *sent.argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    there = None
    try:
        for line in stream.stdout or ():
            if there is None and (ready := READY.match(line)):
                there = Where(ready[1].decode(), ready[2].decode())
                took(there.where)
                continue
            sys.stdout.buffer.write(as_here(line, task, there.where, there.home) if there else line)
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 130
    finally:
        if stream.stdin:
            stream.stdin.close()
        status = stream.wait()
    if there is None:
        # No worktree took the tree; the refs it would have taken are removed with nothing else to.
        push(ROOT, f"{host}:{checkout}", [f":refs/gate/{sent.id}{suffix}" for suffix in ("", "-base", "-at")])
        raise RefError(f"{host} made no worktree of tree {sent.tree[:9]}")
    back = subprocess.run(host_half(host, "back", sent.id), capture_output=True, check=False).stdout
    bring_back(back, sent, there, host, task)
    if sent.fixed:
        print(f"[{task}] {fast.rewrote(sent.fixed)}", flush=True)
    return status


class Room(NamedTuple):
    """A machine's heavy turn as a sender weighs a run against it: its free tokens, its capacity, and the
    worktrees whose shares hold its taken ones (room.py)."""

    free: int
    capacity: int
    holders: frozenset[str] = frozenset()


def asked(host: str, checkout: str) -> Room | None:
    """The second machine's room, asked of the code that reads this machine's, or None when it cannot say."""

    try:
        said = subprocess.run(
            host_half(host, "room", checkout), input=ROOM.read_bytes(), capture_output=True, timeout=ASK, check=False
        )
    except subprocess.TimeoutExpired:
        return None
    lines = said.stdout.decode(errors="replace").splitlines()
    if said.returncode or len(lines) < 2 or not all(count.isdigit() for count in lines[:2]):
        return None
    return Room(int(lines[0]), int(lines[1]), frozenset(lines[2:]))


def demanded(said: str, here: Room) -> int | None:
    """The tokens a run takes, as its task weighed it here before the machine was chosen; None for every token
    of whichever machine runs it, as is a weight of this machine's whole budget, which caps every weight."""

    return None if said == ALL or not said.isdigit() or int(said) >= here.capacity else max(int(said), 1)


def need(demand: int | None, room: Room) -> int:
    return room.capacity if demand is None else min(demand, room.capacity)


def choose(task: str, host: str, demand: int | None, here: Room, there: Room) -> tuple[bool, str]:
    """Whether the run goes there, and the line that says why when it does not start there at once: there
    when it has room for the run now, else here when this machine does, else it waits on the machine that
    lacks the fewest tokens, there on a tie."""

    def said(room: Room) -> str:
        return f"{room.free} of {room.capacity} tokens free"

    takes = "every token" if demand is None else f"{demand} token{'s' * (demand != 1)}"
    if there.free >= need(demand, there):
        return True, ""
    if here.free >= need(demand, here):
        return False, f"[{task}] runs here: it takes {takes}, and {host} has {said(there)}"
    goes = need(demand, there) - there.free <= need(demand, here) - here.free
    return goes, (
        f"[{task}] waits on {host if goes else 'this machine'}: it takes {takes}, {host} has {said(there)}"
        f" and this machine {said(here)}, and {'it' if goes else 'this machine'} lacks the fewest"
    )


def claims() -> Path:
    """Where each run this machine sends holds the tokens it was sent for until the run there holds its share."""

    return lock_file(f"{LOCK}.sent").with_suffix(".d")


@contextlib.contextmanager
def choosing() -> Iterator[None]:
    """One run of this machine's chooses at a time, so runs started together each see the others' claims."""

    path = lock_file(f"{LOCK}.sending")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def claimed(host: str, holders: frozenset[str]) -> int:
    """The tokens there that runs sent from here hold and the room asked there does not count yet: each
    live claim whose worktree there holds no share. A claim nobody holds (its sender ended) is removed."""

    total = 0
    for path in sorted(claims().glob("*.claim")) if claims().is_dir() else ():
        descriptor = os.open(path, os.O_RDONLY)
        try:
            if room.lockable(descriptor):
                path.unlink(missing_ok=True)
                continue
            claimant, where = [*path.read_text().splitlines(), "", ""][:2]
            if claimant == host and where not in holders:
                total += int(path.name.split(".")[0])
        finally:
            os.close(descriptor)
    return total


class Claim:
    """The tokens a run sent there holds until its share there does: a file named for its tokens, locked
    while its sender runs, that names the machine and, once the run took one, the worktree there."""

    def __init__(self, host: str, tokens: int) -> None:
        claims().mkdir(parents=True, exist_ok=True)
        self.host = host
        self.path = claims() / f"{tokens}.{uuid.uuid4().hex}.claim"
        self.descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        os.pwrite(self.descriptor, f"{host}\n".encode(), 0)

    def took(self, where: str) -> None:
        # One write that only lengthens the file, so a reader sees the machine with or without the worktree.
        os.pwrite(self.descriptor, f"{self.host}\n{where}\n".encode(), 0)

    def close(self) -> None:
        self.path.unlink(missing_ok=True)
        os.close(self.descriptor)


def main() -> int:
    task = os.environ["MISE_TASK_NAME"]
    host, _, checkout = os.environ[SETTING].partition(":")
    values = dict(Usage.from_environment().values)
    if inside() or any(values.get(name) == "true" for name in STAYS):
        return HERE
    with choosing():
        there = asked(host, checkout)
        if there is None:
            print(f"[{task}] {host} does not answer, so this runs here", file=sys.stderr, flush=True)
            return HERE
        there = there._replace(free=max(there.free - claimed(host, there.holders), 0))
        here = Room(*room.room(ROOT, gate_held=GATE in held_locks())[:2])
        demand = demanded(sys.argv[1] if len(sys.argv) > 1 else ALL, here)
        goes, why = choose(task, host, demand, here, there)
        if why:
            print(why, file=sys.stderr, flush=True)
        if not goes:
            return HERE
        claim = Claim(host, need(demand, there))
    try:
        return run_there(task, host, checkout, values, claim.took)
    except (RefError, ValueError, OSError) as error:
        print(f"[{task}] {host} could not take the run, so it runs here: {error}", file=sys.stderr, flush=True)
        return HERE
    finally:
        claim.close()


if __name__ == "__main__":
    sys.exit(main())
