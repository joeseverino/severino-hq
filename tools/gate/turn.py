"""The machine's heavy turn (mise's task lock `heavy`): what a run weighs in
tokens of the machine's budget, the share of those tokens it takes beside
other runs, and the gate a landing closes in front of them.

`python -m tools.gate.turn [manage.py test arguments...]` prints the tokens a
run of manage.py test takes. `python -m tools.gate.turn --share <tokens|all>
<bound> <command...>` runs a command in a share. `python -m tools.gate.turn
--landing <command...>` runs a landing's command (mise-tasks/landing). How the
turn is shared, and how a landing goes first: docs/Development.md, Heavy gates
take turns.
"""

import contextlib
import fcntl
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

lazy import django
lazy from django.conf import settings
lazy from django.core.management.commands.test import Command as TestCommand
lazy from django.test.runner import get_max_test_processes
lazy from django.test.utils import get_runner

from tools.gate.room import GATE, LANDED, LOCK, available, capacity, free, free_now, lock_file, recorded, slot
lazy from hq.platform.core.test_runner import drives_a_browser, tests_in

# GATE sorts before LOCK: mise takes a task's locks in sorted order, so a task
# waiting at the gate holds nothing, and a landing's own heavy tasks, which
# inherit it, may still add LOCK after it. A share passes through the gate
# before it takes a token, so a whole run waiting there lets the shares it
# waits for drain and no new one start.
# How mise says which locks a task holds; a task that inherits one takes it as held.
HELD = "MISE_TASK_LOCKS"
# The machine's budget for shares: its declared token count, else what its cores and memory allow.
CAPACITY = capacity(Path(__file__).resolve().parents[2])
# One share takes its tokens at a time, behind a door, so a share of several
# is never starved by shares of one.
DOOR = f"{LOCK}.door"
# A process that runs tests, and one beside which a browser draws (a survey
# holds about 1.2 GB and a core and a half).
ONE = 1
BROWSER = 2
# What a run that needs every token asks for: a share of the whole budget.
ALL = "all"
# Set for every process a share runs, so a run inside it goes ahead.
SHARE = "HQ_TURN_SHARE"
MEASURER = Path(__file__).resolve().parents[2] / "tools" / "lib" / "measured"
# Where the measurer records the peak its tree held, for the next run of the same command (peak_file).
PEAK = "HQ_MEASURED_PEAK"
# mise's own poll of a busy lock, and how often a waiter says it still waits.
FIRST_POLL, MAX_POLL, REPORT_EVERY = 0.1, 1.0, 30.0
# Once a run outlives its bound it is asked to stop, and killed this long after.
GRACE = 5


def in_share(tokens: int, bound: str) -> list[str]:
    """The command that runs what follows it in a share of ``tokens``, stopped once it has held it for ``bound``.

    mise hands the caller's ``usage_*`` variables down with the environment, so the command reads the
    arguments its task was given."""

    return [sys.executable, "-m", "tools.gate.turn", "--share", str(tokens), bound]


def weighed(argv: list[str], root: Path) -> int:
    """The tokens a run of manage.py test on ``argv`` takes, asked in a process of its own: the settings
    ``weigh`` loads start a run that the caller's exec would leave with nothing to remove it."""

    asked = [sys.executable, "-m", "tools.gate.turn", *argv]
    said = subprocess.run(asked, cwd=root, capture_output=True, text=True, check=False).stdout.split()
    return int(said[-1]) if said and said[-1].isdigit() else CAPACITY


@contextlib.contextmanager
def opened(name: str) -> Iterator[int]:
    """The lock file of ``name``, open for the block: closing it lets go of any lock taken on it."""

    path = lock_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only its owner opens it: mise, run as the same user, opens it read-write to take it too.
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def opened_all(names: list[str]) -> Iterator[list[int]]:
    """The lock files of ``names``, each open in a ``with`` of its own for the block."""

    if not names:
        yield []
        return
    with opened(names[0]) as first, opened_all(names[1:]) as rest:
        yield [first, *rest]


def held_locks() -> set[str]:
    return set(filter(None, os.environ.get(HELD, "").split(",")))


def land(command: list[str]) -> int:
    """Run ``command`` with the gate closed to every heavy task outside it, and return its status.

    The command starts at once, its tasks told they hold the gate, and a thread
    takes it beside them. mise polls a busy lock every second at most; this
    blocks in flock(2), which the kernel wakes as the holder lets go, so the
    landing waits behind only the heavy run already in the turn. Its own heavy
    tasks still take turns among themselves on LOCK."""

    record = lock_file(GATE).with_suffix(".json")
    held = held_locks() | {GATE}
    with opened(GATE) as gate, opened(LANDED) as landed:
        threading.Thread(target=hold, args=(gate, landed, record), daemon=True).start()
        try:
            return subprocess.run(command, env=os.environ | {HELD: ",".join(sorted(held))}, check=False).returncode
        except KeyboardInterrupt:
            return 130
        finally:
            # The record goes first, while the lock still keeps the next holder from writing its own.
            record.unlink(missing_ok=True)


def hold(gate: int, landed: int, record: Path) -> None:
    """Take the gate, then LANDED, which says a landing holds it, then say who holds it the way mise's holders
    do, for the line its waiters print."""

    fcntl.flock(gate, fcntl.LOCK_EX)
    fcntl.flock(landed, fcntl.LOCK_EX)
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

    if not (record := recorded(name)):
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

    def until(self, taken: Callable[[], bool], what: Callable[[], str], fits: Callable[[], bool] = bool) -> None:
        """Poll ``taken()`` until it is true, saying ``what()`` while it is not, and raise Fits once ``fits()``."""

        poll = FIRST_POLL
        while not taken():
            if fits():
                raise Fits
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


class Fits(Exception):
    """The peak a share's command last recorded fits the memory the OS has available now."""


def peak_file(command: list[str]) -> Path:
    """Where the measurer records the peak of a share's ``command`` run by this task: by the task and the
    words after the command's first, its path in one checkout, so every worktree reads the same record."""

    return lock_file("\0".join(["peak", task_name(), *command[1:]])).with_suffix(".peak")


def fitting(tokens: int, peak: Path) -> Callable[[], bool]:
    """Whether a share of ``tokens`` runs on no tokens now: one of fewer than every token whose command's
    recorded peak fits the memory the OS has available. A run that needs every token may hold memory outside
    its tree (a Docker VM), so it never does."""

    def fits() -> bool:
        try:
            need = int(peak.read_text())
        except OSError, ValueError:
            return False
        return need <= available()

    return fits if tokens < CAPACITY else bool


def backfills(tokens: int, fits: Callable[[], bool]) -> bool:
    """Whether a share of ``tokens`` passes the gate a landing holds now, as EASY backfill (Slurm's backfill
    scheduler) starts a smaller job on idle resources ahead of the reserved one: a share of fewer than every
    token, on tokens free now or on none when it fits. Tokens it still takes behind the door, where a landing's
    own share collects every token it waits for, so none passes it. A landing takes LANDED only once it holds
    the gate, and the kernel lets it go as the landing ends, so a whole run waiting at the gate is never passed."""

    landing = tokens < CAPACITY and not free_now(LANDED, fcntl.LOCK_EX)
    return landing and (free(CAPACITY, gate_held=True) >= tokens or fits())


@contextlib.contextmanager
def share_held(tokens: int, waiting: Waiting, fits: Callable[[], bool]) -> Iterator[None]:
    """Hold a share for the block: ``tokens`` of the machine's, or none once ``fits()`` while it waits for them
    (Fits), then LOCK shared, which a whole run's exclusive hold keeps it from."""

    with contextlib.ExitStack() as held:
        try:
            held.enter_context(tokens_taken(tokens, waiting, fits))
        except Fits:
            give_back()
            tokens = 0
        with opened(LOCK) as heavy:
            try:
                waiting.until(lambda: flocked(heavy, fcntl.LOCK_SH), lambda: on(LOCK))
                record(LOCK)
                waiting.done(tokens)
                yield
            finally:
                give_back()


@contextlib.contextmanager
def tokens_taken(tokens: int, waiting: Waiting, fits: Callable[[], bool]) -> Iterator[None]:
    """Hold ``tokens`` for the block: past the gate, then that many slots behind the door. Each lock file is
    open in a block of its own; a slot it opened and did not take holds no lock, and Fits lets go of all."""

    if GATE not in held_locks():
        # A turnstile: passed, it is let go at once, so only a run that holds it stops the shares behind it.
        with opened(GATE) as gate:
            waiting.until(lambda: flocked(gate, fcntl.LOCK_SH) or backfills(tokens, fits), lambda: on(GATE))
    mine: list[int] = []
    with opened_all([slot(n) for n in range(CAPACITY)]) as slots:
        with opened(DOOR) as door:

            def enough() -> bool:
                for n, descriptor in enumerate(slots):
                    if len(mine) < tokens and descriptor not in mine and flocked(descriptor, fcntl.LOCK_EX):
                        mine.append(descriptor)
                        record(slot(n))
                return len(mine) == tokens

            def holders() -> str:
                others = [holder(slot(n)) for n, descriptor in enumerate(slots) if descriptor not in mine]
                return f"a share of {tokens} of lock {LOCK}'s {CAPACITY}, " + "; ".join(
                    sorted({o for o in others if o})
                )

            waiting.until(lambda: flocked(door, fcntl.LOCK_EX), lambda: on(DOOR), fits)
            try:
                record(DOOR)
                waiting.until(enough, holders, fits)
            finally:
                lock_file(DOOR).with_suffix(".json").unlink(missing_ok=True)
        yield


def give_back() -> None:
    """Take this share's records back, while its locks still keep the next holder from writing its own."""

    others = []
    for n in range(CAPACITY):
        held_by = recorded(slot(n))
        if held_by.get("pid") == os.getpid():
            lock_file(slot(n)).with_suffix(".json").unlink(missing_ok=True)
        elif held_by:
            others.append(held_by)
    # LOCK's record names one share that holds it; this one's goes to another still holding it, if any.
    if recorded(LOCK).get("pid") == os.getpid():
        heavy = lock_file(LOCK).with_suffix(".json")
        if others:
            heavy.write_text(json.dumps(others[0]))
        else:
            heavy.unlink(missing_ok=True)


def share(tokens: int, bound: str, command: list[str]) -> int:
    """Run ``command`` in a share of ``tokens``, no more than the machine holds, and return its status,
    stopping it once it has held the share for ``bound``. Inside the turn or a share it runs at once. It runs
    under the measurer, so it ends with the line on what its process tree spent.

    The command is told it holds the gate and the turn the way mise tells a task (MISE_TASK_LOCKS), so a task
    under it that takes the lock in its header goes ahead inside the share rather than waiting forever on the
    share its own run holds, so a run that may start one takes every token."""

    peak = peak_file(command)
    if not os.environ.get("HQ_MEASURED"):
        command = [str(MEASURER), *command]
    if inside():
        return subprocess.run(command, check=False).returncode
    tokens = min(tokens, CAPACITY)
    held = ",".join(sorted(held_locks() | {GATE, LOCK}))
    env = os.environ | {SHARE: str(tokens), HELD: held, PEAK: str(peak)}
    try:
        with share_held(tokens, Waiting(), fitting(tokens, peak)):
            return run_bounded(command, seconds(bound), bound, env)
    except TimeoutError as error:
        print(f"[{task_name()}] ERROR {error}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130


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


def weigh(argv: list[str]) -> int:
    """The tokens the run manage.py test makes of ``argv`` takes: one for each process it runs tests in, two
    where a browser draws beside it, and never more than the machine holds."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    serial = False
    try:
        django.setup()
        options = vars(TestCommand().create_parser("manage.py", "test").parse_args(argv))
        labels = options.pop("args")
        # manage.py test runs in one process unless asked for more (its default is 0).
        serial = options["parallel"] in {0, 1}
        # The workers manage.py test asks for (its handle() resolves "auto" so).
        workers = get_max_test_processes() if options["parallel"] == "auto" else options["parallel"]
        # A serial run is one process however many tests it loads. The whole suite or a tree is not loaded to
        # be weighed: its test classes outnumber its workers, any of which may draw a browser.
        if not serial and (not labels or any(map(names_a_tree, labels))):
            return min(workers * BROWSER, CAPACITY)
        # Loaded without a word: the run says what it found.
        runner = get_runner(settings, options["testrunner"])(**(options | {"parallel": workers, "verbosity": 0}))
        suite = runner.build_suite(labels)
        # build_suite cuts the workers to the test classes it found, as the run does.
        tokens = max(runner.parallel, 1) * (BROWSER if any(map(drives_a_browser, tests_in(suite))) else ONE)
        return min(tokens, CAPACITY)
    except Exception, SystemExit:  # noqa: BLE001 - manage.py says what stopped it, in its share
        # A serial run that cannot load its tests fails in one process.
        return ONE if serial else CAPACITY


if __name__ == "__main__":
    if sys.argv[1:2] == ["--landing"]:
        sys.exit(land(sys.argv[2:]))
    if sys.argv[1:2] == ["--share"]:
        sys.exit(share(CAPACITY if sys.argv[2] == ALL else int(sys.argv[2]), sys.argv[3], sys.argv[4:]))
    print(weigh(sys.argv[1:]))
