"""The machine's heavy turn (mise's task lock `heavy`): what a run of
manage.py test weighs, the share a one-process run takes beside others, and
the gate a landing closes in front of them.

`python -m tools.gate.turn [manage.py test arguments...]` prints the run's
weight: `whole`, or the tokens of the share it takes.
`python -m tools.gate.turn --share <tokens> <bound> <command...>` runs a
command in a share. `python -m tools.gate.turn --landing <command...>` runs a
landing's command (mise-tasks/landing). How the turn is shared, and how a
landing goes first: docs/Development.md, Heavy gates take turns.
"""

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

lazy import django
lazy from django.conf import settings
lazy from django.core.management.commands.test import Command as TestCommand
lazy from django.test.runner import get_max_test_processes
lazy from django.test.utils import get_runner

lazy from hq.platform.core.test_runner import drives_a_browser, tests_in

# The lock a run that takes the whole machine holds exclusively, and the task
# that runs a command in it for a task that weighs its own run (mise-tasks/turn).
# A share holds it shared, so the two never overlap.
LOCK = "heavy"
IN_TURN = ["mise", "run", "turn", "--"]
# The lock every heavy task takes before the turn, which a landing holds for
# its command's lifetime. It sorts before LOCK: mise takes a task's locks in
# sorted order, so a task waiting here holds nothing, and a landing's own heavy
# tasks, which inherit it, may still add LOCK after it. A share passes through
# it before it takes a token, so a whole run waiting here lets the shares it
# waits for drain and no new one start.
GATE = "gate"
# How mise says which locks a task holds; a task that inherits one takes it as held.
HELD = "MISE_TASK_LOCKS"
# The machine's budget for shares, in tokens of about a core and 600 MB each:
# four hold two surveys or four one-process test runs, about 2.4 GB and five
# cores, beside the dev server, a browser and the agents on an 8 GB, 8-core
# Mac. One share takes its tokens at a time, behind a door, so a share of
# several is never starved by shares of one.
CAPACITY = 4
DOOR = f"{LOCK}.door"
# A process that runs tests, and one beside which a browser draws (a survey
# holds about 1.2 GB and a core and a half).
ONE = 1
BROWSER = 2
# Set for every process a share runs, so a run inside it goes ahead.
SHARE = "HQ_TURN_SHARE"
MEASURER = Path(__file__).resolve().parents[2] / "tools" / "lib" / "measured"
# mise's own poll of a busy lock, and how often a waiter says it still waits.
FIRST_POLL, MAX_POLL, REPORT_EVERY = 0.1, 1.0, 30.0
# Once a run outlives its bound it is asked to stop, and killed this long after.
GRACE = 5


def in_turn(bound: str) -> list[str]:
    """The command that runs what follows it in the turn, stopped once it has held the turn for ``bound`` (30m, 2h).

    mise gives the turn task its own parse, dropping the caller's ``usage_*``
    variables, so the command is handed them again: a task's run in the turn
    reads the arguments it was given."""

    parse = [f"{name}={value}" for name, value in os.environ.items() if name.startswith("usage_")]
    return ["env", f"HQ_TURN_BOUND={bound}", *IN_TURN, "env", *parse]


def in_share(tokens: int, bound: str) -> list[str]:
    """The command that runs what follows it in a share of ``tokens``, stopped once it has held it for ``bound``."""

    return [sys.executable, "-m", "tools.gate.turn", "--share", str(tokens), bound]


def lock_file(name: str) -> Path:
    """The file mise flocks for the task lock ``name``: under its state directory,
    named for the lock's hash (src/task/task_lock.rs, ``LockKey::path``)."""

    state = os.environ.get("MISE_STATE_DIR") or Path(
        os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state", "mise"
    )
    return Path(state, "task-locks", hashlib.sha256(name.encode()).hexdigest()[:16] + ".lock")


def opened(name: str) -> int:
    path = lock_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only its owner opens it: mise, run as the same user, opens it read-write to take it too.
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def held_locks() -> set[str]:
    return set(filter(None, os.environ.get(HELD, "").split(",")))


def land(command: list[str]) -> int:
    """Run ``command`` with the gate closed to every heavy task outside it, and return its status.

    The command starts at once, its tasks told they hold the gate, and a thread
    takes it beside them. mise polls a busy lock every second at most; this
    blocks in flock(2), which the kernel wakes as the holder lets go, so the
    landing waits behind only the heavy run already in the turn. Its own heavy
    tasks still take turns among themselves on LOCK."""

    gate = opened(GATE)
    record = lock_file(GATE).with_suffix(".json")
    threading.Thread(target=hold, args=(gate, record), daemon=True).start()
    held = held_locks() | {GATE}
    try:
        return subprocess.run(command, env=os.environ | {HELD: ",".join(sorted(held))}, check=False).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        # The record goes first, while the lock still keeps the next holder from writing its own.
        record.unlink(missing_ok=True)
        os.close(gate)


def hold(gate: int, record: Path) -> None:
    """Take the gate, then say who holds it the way mise's holders do, for the line its waiters print."""

    fcntl.flock(gate, fcntl.LOCK_EX)
    held_by = {"pid": os.getpid(), "task": "landing", "root": str(Path.cwd()), "started_at": int(time.time())}
    record.write_text(json.dumps(held_by))


def inside() -> bool:
    """Whether this process runs under a task that holds the turn or in a share, so a run goes ahead at once."""

    return LOCK in held_locks() or SHARE in os.environ


def seconds(duration: str) -> float:
    """A duration as mise writes one (500ms, 1s, 30m, 2h, 1h30m) in seconds."""

    units = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", duration)
    if not parts or "".join(number + unit for number, unit in parts) != duration:
        raise ValueError(f"not a duration: {duration!r}")
    return sum(float(number) * units[unit] for number, unit in parts)


def human(spent: float) -> str:
    """A wait or a holder's age as mise prints one."""

    whole = int(spent)
    if whole < 60:
        return f"{spent:.1f}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def task_name() -> str:
    return os.environ.get("MISE_TASK_NAME") or "share"


def holder(name: str) -> str:
    """Who holds the lock ``name``, as its holder recorded it (mise's ``HolderRecord``), or nothing."""

    try:
        record = json.loads(lock_file(name).with_suffix(".json").read_text())
    except OSError, ValueError:
        return ""
    root = str(record.get("root") or "")
    home = str(Path.home())
    where = f" in ~{root.removeprefix(home)}" if root.startswith(home) else f" in {root}" if root else ""
    return f"held by {record['task']}{where} (pid {record['pid']}, for {human(time.time() - record['started_at'])})"


def on(name: str) -> str:
    """What a wait for the lock ``name`` is on: the lock, and its holder when one has said who it is."""

    said = holder(name)
    return f"lock {name}, {said}" if said else f"lock {name}"


def record(name: str) -> None:
    """Say who holds ``name`` the way mise's holders do."""

    held_by = {
        "pid": os.getpid(),
        "task": task_name(),
        "root": os.environ.get("MISE_CONFIG_ROOT") or str(Path.cwd()),
        "started_at": int(time.time()),
    }
    lock_file(name).with_suffix(".json").write_text(json.dumps(held_by))


class Waiting:
    """One share's wait for its locks: polled as mise polls, said every 30 seconds, and given up after
    MISE_TASK_LOCK_TIMEOUT, as mise gives up on a lock."""

    def __init__(self) -> None:
        self.began = time.monotonic()
        self.said = 0.0
        limit = os.environ.get("MISE_TASK_LOCK_TIMEOUT")
        self.limit = seconds(limit) if limit else None
        self.waited = False

    def until(self, taken: Callable[[], bool], what: Callable[[], str]) -> None:
        """Poll ``taken()`` until it is true, saying ``what()`` while it is not."""

        poll = FIRST_POLL
        while not taken():
            spent = time.monotonic() - self.began
            if self.limit is not None and spent >= self.limit:
                raise TimeoutError(f"timed out after {human(self.limit)} waiting for {what()}")
            if not self.waited or time.monotonic() - self.said >= REPORT_EVERY:
                print(f"[{task_name()}] waiting for {what()}", file=sys.stderr, flush=True)
                self.said, self.waited = time.monotonic(), True
            time.sleep(poll)
            poll = min(poll * 2, MAX_POLL)

    def done(self, tokens: int) -> None:
        if self.waited:
            print(
                f"[{task_name()}] got a share of {tokens} after {human(time.monotonic() - self.began)}",
                file=sys.stderr,
                flush=True,
            )


def flocked(fd: int, mode: int) -> bool:
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def take_share(tokens: int, waiting: Waiting) -> list[int]:
    """Take a share of ``tokens``: past the gate, ``tokens`` slots behind the door, then LOCK shared.
    Returns the descriptors that hold it; closing them gives it back."""

    held: list[int] = []
    try:
        if GATE not in held_locks():
            gate = opened(GATE)
            held.append(gate)
            waiting.until(lambda: flocked(gate, fcntl.LOCK_SH), lambda: on(GATE))
            # A turnstile: passed, it is let go at once, so only a run that holds it stops the shares behind it.
            held.remove(gate)
            os.close(gate)
        door = opened(DOOR)
        held.append(door)
        slots = [opened(f"{LOCK}.share.{n}") for n in range(CAPACITY)]
        held += slots
        mine: list[int] = []

        def enough() -> bool:
            for n, slot in enumerate(slots):
                if len(mine) < tokens and slot not in mine and flocked(slot, fcntl.LOCK_EX):
                    mine.append(slot)
                    record(f"{LOCK}.share.{n}")
            return len(mine) == tokens

        def holders() -> str:
            others = [holder(f"{LOCK}.share.{n}") for n, slot in enumerate(slots) if slot not in mine]
            return f"a share of {tokens} of lock {LOCK}'s {CAPACITY}, " + "; ".join(sorted({o for o in others if o}))

        waiting.until(lambda: flocked(door, fcntl.LOCK_EX), lambda: on(DOOR))
        record(DOOR)
        waiting.until(enough, holders)
        for slot in slots:
            if slot not in mine:
                held.remove(slot)
                os.close(slot)
        lock_file(DOOR).with_suffix(".json").unlink(missing_ok=True)
        held.remove(door)
        os.close(door)
        heavy = opened(LOCK)
        held.append(heavy)
        waiting.until(lambda: flocked(heavy, fcntl.LOCK_SH), lambda: on(LOCK))
        record(LOCK)
        waiting.done(tokens)
        return held
    except BaseException:
        for fd in held:
            os.close(fd)
        raise


def recorded(name: str) -> str:
    """The record of who holds ``name``, or nothing."""

    try:
        return lock_file(name).with_suffix(".json").read_text()
    except OSError:
        return ""


def ours(text: str) -> bool:
    """Whether a holder's record names this process; a record cut short as it was written names none."""

    try:
        found = json.loads(text)
    except ValueError:
        return False
    return isinstance(found, dict) and found.get("pid") == os.getpid()


def give_back(held: list[int]) -> None:
    """Close what holds a share, its records first, while the locks still keep the next holder from writing."""

    others = []
    for n in range(CAPACITY):
        text = recorded(f"{LOCK}.share.{n}")
        if ours(text):
            lock_file(f"{LOCK}.share.{n}").with_suffix(".json").unlink(missing_ok=True)
        elif text:
            others.append(text)
    # LOCK's record names one share that holds it; this one's goes to another still holding it, if any.
    if ours(recorded(LOCK)):
        heavy = lock_file(LOCK).with_suffix(".json")
        if others:
            heavy.write_text(others[0])
        else:
            heavy.unlink(missing_ok=True)
    for fd in held:
        os.close(fd)


def share(tokens: int, bound: str, command: list[str]) -> int:
    """Run ``command`` in a share of ``tokens`` and return its status, stopping it once it has held the share
    for ``bound``. Inside the turn or a share it runs at once. It runs under the measurer, so it ends with the
    line on what its process tree spent."""

    if not os.environ.get("HQ_MEASURED"):
        command = [str(MEASURER), *command]
    if inside():
        return subprocess.run(command, check=False).returncode
    try:
        held = take_share(min(tokens, CAPACITY), Waiting())
    except TimeoutError as error:
        print(f"[{task_name()}] ERROR {error}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    try:
        return run_bounded(command, seconds(bound), bound, os.environ | {SHARE: str(tokens)})
    finally:
        give_back(held)


def run_bounded(command: list[str], limit: float, bound: str, env: dict[str, str]) -> int:
    """Run ``command`` in a process group of its own, which a stop reaches whole, and stop it past ``limit``."""

    child = subprocess.Popen(command, env=env, process_group=0)

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signum)

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, forward)
    try:
        return child.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        print(f"[{task_name()}] ERROR timed out after {bound}", file=sys.stderr, flush=True)
        forward(signal.SIGTERM, None)
        try:
            child.wait(timeout=GRACE)
        except subprocess.TimeoutExpired:
            forward(signal.SIGKILL, None)
            child.wait()
        return 124


def names_a_tree(label: str) -> bool:
    """Whether ``label`` names a directory or a package rather than a module or something in one."""

    if os.sep in label or Path(label).is_dir():
        return True
    try:
        spec = importlib.util.find_spec(label)
    except ImportError, ValueError:
        # Its parent is a module, so the label names something in that module.
        return False
    return spec is not None and spec.submodule_search_locations is not None


def weigh(argv: list[str]) -> int | None:
    """The tokens of the share the run manage.py test makes of ``argv`` takes, one for each process it runs
    tests in and two where a browser draws beside it, or None for one that takes the whole turn: as many as
    the machine holds, the whole suite or a tree."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    serial = False
    try:
        django.setup()
        options = vars(TestCommand().create_parser("manage.py", "test").parse_args(argv))
        labels = options.pop("args")
        serial = options["parallel"] == 1
        # A serial run is one process however many tests it loads.
        if not serial and (not labels or any(map(names_a_tree, labels))):
            return None
        # The workers manage.py test asks for (its handle() resolves "auto" so),
        # loaded without a word: the run says what it found.
        workers = get_max_test_processes() if options["parallel"] == "auto" else options["parallel"]
        runner = get_runner(settings, options["testrunner"])(**(options | {"parallel": workers, "verbosity": 0}))
        suite = runner.build_suite(labels)
        # build_suite cuts the workers to the test classes it found, as the run does.
        tokens = max(runner.parallel, 1) * (BROWSER if any(map(drives_a_browser, tests_in(suite))) else ONE)
        return tokens if tokens < CAPACITY else None
    except Exception, SystemExit:  # noqa: BLE001 - manage.py says what stopped it, in the turn
        # A serial run that cannot load its tests fails in one process.
        return ONE if serial else None


if __name__ == "__main__":
    if sys.argv[1:2] == ["--landing"]:
        sys.exit(land(sys.argv[2:]))
    if sys.argv[1:2] == ["--share"]:
        sys.exit(share(int(sys.argv[2]), sys.argv[3], sys.argv[4:]))
    weight = weigh(sys.argv[1:])
    print("whole" if weight is None else weight)
