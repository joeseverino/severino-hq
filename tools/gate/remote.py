"""Where a gate a person types runs: on the second machine HQ_GATE_REMOTE
names, when it answers, else here (docs/Development.md, Gates on a second
machine). `python -m tools.gate.remote`, which tools/lib/remote.sh runs, runs
the task mise is running there and exits with its status, or exits HERE for
the task to run here.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from hq.platform.core.tests.task import RefError, Usage, commit_of, git
from tools.gate import chain, testmap
from tools.gate.tree import working_tree
from tools.gate.turn import inside
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
# Tasks that run the composed suite themselves when their change calls for it (fast's extension suites).
COMPOSING = {"fast"}
# What this run sets that changes what a gate does, handed to the run there.
CARRIED = (
    *("HQ_BROWSER_CHANNEL", "HQ_BROWSER_ENGINE", "HQ_BROWSER_FACE", "HQ_BROWSER_SHARD", "HQ_GATE_OUTPUT"),
    *("HQ_LOCAL_PLUGINS", "HQ_TEST_STALL_SECONDS", "HYPOTHESIS_PROFILE", "REQUIRE_COMPOSED", "TEST_PYTHONS"),
)
# Every other variable a gate reads, by why the run there reads its own; test_remote.py holds the two lists
# to what the gates read.
LEFT = {
    "a gate sets it for what it runs, there as here": (
        *("HQ_API_RECORD_EXAMPLES_TO", "HQ_EXTENSIONS_SAID", "HQ_FAST_SOURCE", "HQ_GATE_DETAIL", "HQ_GATE_HERE"),
        *("HQ_GATE_RERUN", "HQ_GATE_TREE", "HQ_GATE_VERDICTS", "HQ_LOOK_SHARE", "HQ_LOOK_SHARES", "HQ_MEASURED"),
        *("HQ_TEST_MARKS", "HQ_TEST_REPORTS", "HQ_TURN_BOUND", "HQ_TURN_SHARE", "MCP_PROBE_DEFER_BUILD"),
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
]
HOST_HALF = Path(__file__).with_name("remote-host.sh")


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
    command = ["git", "push", "--quiet", "--force", remote, *refspecs]
    done(command, root, env=os.environ | {"GIT_SSH_COMMAND": shlex.join(SSH)})


def host_half(host: str, step: str, *args: str) -> list[str]:
    return [*SSH, host, shlex.join(["sh", "-c", HOST_HALF.read_text(), "remote-host.sh", step, *args])]


def bring_back(archive: bytes, sent: Sent, host: str, task: str) -> None:
    """Keep what the run there wrote: each verdict that names the tree sent, the logs and looks, and the pass
    it recorded, the verdicts and logs naming this checkout where they named the worktree there."""

    tree = sent.tree
    with tempfile.TemporaryDirectory() as folder:
        back = Path(folder)
        with tarfile.open(fileobj=io.BytesIO(archive)) as kept:
            kept.extractall(back, filter="data")
        for written in (*back.glob("var/gate/*.txt"), *back.glob("var/gate/*.log")):
            written.write_bytes(here(written.read_bytes(), sent.where, sent.home))
        for verdict in sorted(back.glob("var/gate/*.txt")):
            if verdict.read_text().splitlines()[:1] != [TREE_LINE.format(tree)]:
                print(f"[{task}] {host}'s var/gate/{verdict.name} names another tree, so it is not kept", flush=True)
                verdict.unlink()
            elif (mine := ROOT / "var/gate" / verdict.name).is_file():
                mine.replace(mine.with_suffix(".prev.txt"))
        for part in ("var/gate", "var/look"):
            if (back / part).is_dir():
                shutil.copytree(back / part, ROOT / part, dirs_exist_ok=True)
        fast = back / "var/gate/fast.txt"
        if (back / "var/back" / chain.PASSES[1] / tree).is_file() and fast.is_file():
            chain.record_elsewhere(tree, fast.read_text(), host)
        if (found := back / "var/back" / testmap.MAPS / f"{tree}.json").is_file():
            chain.maps().mkdir(parents=True, exist_ok=True)
            shutil.copy2(found, chain.maps() / found.name)


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
    """A tree made ready there: its worktree there and that machine's home, the arguments that ask for the
    task again, and the extension tree a composed run composes, with its packages' paths in it."""

    tree: str
    where: str
    home: str
    argv: list[str]
    extension: str
    packages: str


def composes(task: str, values: dict[str, str]) -> bool:
    """Whether the run may compose the extension set: the composed suite, and fast, which runs it when a change
    reaches the extensions."""

    return task in COMPOSING or values.get("composed") == "true"


def send(task: str, host: str, checkout: str, values: dict[str, str]) -> Sent:
    """Send this checkout's tree, and the commits its arguments name, and make it ready there."""

    tree = working_tree(ROOT)
    commit = gate_commit(ROOT, tree)
    refs = {f"refs/gate/{tree}": commit}
    for name in sorted(REFS & values.keys()):
        values[name] = refs[f"refs/gate/{tree}-{name}"] = commit_of(values[name])
    # fast reads its default base from the passes and branches here, which the machine there lacks.
    if task == "fast" and "base" not in values:
        values["base"] = chain.fast_base(None)[0]
    remote = f"{host}:{checkout}"
    push(ROOT, remote, [f"{sha}:{ref}" for ref, sha in refs.items()])
    # A run that composes composes this machine's extension checkout, sent as the tree is, from its clone beside
    # HQ's there; one that cannot be sent leaves the run here, never composing that machine's own.
    extension, packages = [], ""
    if composes(task, values) and (paths := os.environ.get("HQ_LOCAL_PYTHONPATH", "")):
        top = Path(git("rev-parse", "--show-toplevel", root=Path(paths.split(":")[0]))[0])
        packages = ":".join(str(Path(path).relative_to(top)) for path in paths.split(":"))
        extension = [str(PurePosixPath(checkout).parent / top.name), working_tree(top)]
        extension.append(gate_commit(top, extension[1]))
        push(top, f"{host}:{extension[0]}", [f"{extension[2]}:refs/gate/{extension[1]}"])
    argv = words(json.loads(done(["mise", "tasks", "info", "--json", task]))["usage_spec"]["cmd"], values)
    # A file the run reads that no commit holds (a probe under var/) goes with the tree.
    extras = chain.answered("check-ignore", "--", *(word for word in argv if (ROOT / word).is_file()))
    sent = io.BytesIO()
    with tarfile.open(fileobj=sent, mode="w") as archive:
        for path in extras:
            archive.add(ROOT / path, arcname=path)
    try:
        where, home = done(host_half(host, "prepare", checkout, tree, commit, *extension), input=sent.getvalue()).split(
            "\n"
        )
    except RefError:
        # The worktree there would have taken the refs; without one, nothing else removes them.
        push(ROOT, remote, [f":{ref}" for ref in refs])
        raise
    # fast's chain: the trees passed here and the maps of what their tests ran, so it selects as it would here.
    if folders := [str(folder) for folder in (chain.passes(), chain.maps()) if folder.is_dir()]:
        done(["rsync", "-a", "-e", shlex.join(SSH), *folders, f"{remote}/.git/severino/"])
    return Sent(tree, where, home, argv, extension[1] if extension else "", packages)


def run_there(task: str, host: str, checkout: str, values: dict[str, str]) -> int:
    sent = send(task, host, checkout, values)
    print(f"[{task}] runs on {host}, tree {sent.tree[:9]}", file=sys.stderr, flush=True)
    # The run there writes to no terminal, so it is told how this run shows a gate's output.
    carried = {OUTPUT: "all" if shows_all(sys.stdout) else "short"} | {
        name: value for name, value in os.environ.items() if name in CARRIED
    }
    handed = [f"{name}={value}" for name, value in carried.items()]
    command = host_half(host, "run", sent.tree, sent.extension, sent.packages, "env", *handed, "mise", "run", task)
    # Its input closing is what stops the run there.
    stream = subprocess.Popen(
        [*command, *sent.argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        for line in stream.stdout or ():
            sys.stdout.buffer.write(as_here(line, task, sent.where, sent.home))
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 130
    finally:
        if stream.stdin:
            stream.stdin.close()
        status = stream.wait()
    back = subprocess.run(host_half(host, "back", sent.tree), capture_output=True, check=False).stdout
    bring_back(back, sent, host, task)
    return status


def main() -> int:
    task = os.environ["MISE_TASK_NAME"]
    host, _, checkout = os.environ[SETTING].partition(":")
    values = dict(Usage.from_environment().values)
    if inside() or any(values.get(name) == "true" for name in STAYS):
        return HERE
    if subprocess.run([*SSH, host, "true"], capture_output=True, check=False).returncode:
        print(f"[{task}] {host} does not answer, so this runs here", file=sys.stderr, flush=True)
        return HERE
    try:
        return run_there(task, host, checkout, values)
    except (RefError, ValueError, OSError) as error:
        print(f"[{task}] {host} could not take the run, so it runs here: {error}", file=sys.stderr, flush=True)
        return HERE


if __name__ == "__main__":
    sys.exit(main())
