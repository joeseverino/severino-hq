"""A machine's heavy turn as files: the locks tools/gate/turn.py takes, the
machine's budget of tokens, and how many of them are free now.

Stdlib only, at the python3 of a gate machine: tools/gate/remote.py sends this
file to the second machine over SSH (remote-host.sh room) and reads its one
line, `<free> <capacity>`, as it reads this machine's from `room()`, so both
machines answer from the same code.
"""

import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path

# The lock a task that needs the whole machine holds exclusively, by its own
# mise header or template; a share holds it shared, so the two never overlap.
LOCK = "heavy"
# The lock every heavy task takes before the turn, which a landing holds for
# its command's lifetime (tools/gate/turn.py, GATE's comment).
GATE = "gate"
# The machine's measured token count, set once on it in its clone's git config
# (docs/Development.md, Gates on a second machine).
DECLARED = "hq.gateTokens"


def lock_file(name: str) -> Path:
    """The file mise flocks for the task lock ``name``: under its state directory,
    named for the lock's hash (src/task/task_lock.rs, ``LockKey::path``)."""

    state = os.environ.get("MISE_STATE_DIR") or Path(
        os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state", "mise"
    )
    return Path(state, "task-locks", hashlib.sha256(name.encode()).hexdigest()[:16] + ".lock")


def slot(n: int) -> str:
    """The lock of a share's ``n``th token."""

    return f"{LOCK}.share.{n}"


def budget(cores: int, memory: int) -> int:
    """A machine's tokens when it declares none, each about a core and 600 MB: one
    for each core, and no more than one for each 2 GB it holds, which leaves room
    for the dev server, a browser and the agents. It counts every core alike, so
    it overstates a machine whose cores are slow or throttled."""

    return max(1, min(cores, memory // 2**31))


def capacity(root: Path) -> int:
    """The machine's budget for shares: the token count its clone at ``root`` declares, else ``budget``."""

    try:
        said = subprocess.run(
            ["git", "-C", str(root), "config", "--get", DECLARED], capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:
        said = ""
    if not said:
        return budget(os.cpu_count() or 1, os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    if not said.isdigit() or int(said) < 1:
        raise ValueError(f"git config {DECLARED} is {said!r}, not a count of tokens")
    return int(said)


def lockable(descriptor: int, mode: int = fcntl.LOCK_EX) -> bool:
    """Whether the open file ``descriptor`` could be locked in ``mode`` now; the lock is kept until it closes."""

    try:
        fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def free_now(name: str, mode: int) -> bool:
    """Whether the lock ``name`` could be taken in ``mode`` now. A lock file no run has made is free; one
    taken here is let go at once."""

    try:
        descriptor = os.open(lock_file(name), os.O_RDONLY)
    except FileNotFoundError:
        return True
    try:
        return lockable(descriptor, mode)
    finally:
        os.close(descriptor)


def free(tokens: int, *, gate_held: bool = False) -> int:
    """How many of the machine's ``tokens`` a share could take now: none while a run holds the turn whole or
    the gate is closed (a landing, or a whole run waiting for the turn), unless this run holds the gate."""

    if not free_now(LOCK, fcntl.LOCK_SH) or not (gate_held or free_now(GATE, fcntl.LOCK_SH)):
        return 0
    return sum(free_now(slot(n), fcntl.LOCK_EX) for n in range(tokens))


def holders(tokens: int) -> set[str]:
    """The folders the runs holding a share's tokens run in, as each share recorded its holder
    (tools/gate/turn.py, record)."""

    return {root for n in range(tokens) if not free_now(slot(n), fcntl.LOCK_EX) and (root := recorded_root(slot(n)))}


def recorded_root(name: str) -> str:
    """The folder the holder of ``name`` recorded, or nothing: a record cut short as it is written names none."""

    try:
        found = json.loads(lock_file(name).with_suffix(".json").read_text())
    except (OSError, ValueError):
        return ""
    return str(found.get("root") or "") if isinstance(found, dict) else ""


def room(root: Path, *, gate_held: bool = False) -> tuple[int, int, set[str]]:
    """This machine's free tokens, its capacity, and the folders of the runs holding its taken tokens."""

    tokens = capacity(root)
    return free(tokens, gate_held=gate_held), tokens, holders(tokens)


if __name__ == "__main__":
    # Run from the clone whose git config declares the machine's tokens.
    free_tokens, tokens, folders = room(Path.cwd())
    print(free_tokens, tokens, *sorted(folders), sep="\n")
