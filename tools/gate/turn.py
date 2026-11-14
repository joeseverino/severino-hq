"""The machine's heavy turn (mise's task lock `heavy`): whether a run of
manage.py test is narrow enough to go ahead without it, and the gate a landing
closes in front of it.

`python -m tools.gate.turn [manage.py test arguments...]` exits 0 for a narrow
run and 1 for a heavy one, for every task that runs tests on labels.
`python -m tools.gate.turn --landing <command...>` runs a landing's command
(mise-tasks/landing). What narrow means, and how a landing goes first:
docs/Development.md, Heavy gates take turns.
"""

import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import django
from django.conf import settings
from django.core.management.commands.test import Command as TestCommand
from django.test.runner import get_max_test_processes
from django.test.utils import get_runner

from hq.platform.core.test_runner import drives_a_browser, tests_in

# The lock every heavy task takes, and the task that runs a command in it for
# a task that weighs its own run (mise-tasks/turn).
LOCK = "heavy"
IN_TURN = ["mise", "run", "turn", "--"]
# The lock every heavy task takes before the turn, which a landing holds for
# its command's lifetime. It sorts before LOCK: mise takes a task's locks in
# sorted order, so a task waiting here holds nothing, and a landing's own heavy
# tasks, which inherit it, may still add LOCK after it.
GATE = "gate"
# How mise says which locks a task holds; a task that inherits one takes it as held.
HELD = "MISE_TASK_LOCKS"


def in_turn(bound: str) -> list[str]:
    """The command that runs what follows it in the turn, stopped once it has held the turn for ``bound`` (30m, 2h).

    mise gives the turn task its own parse, dropping the caller's ``usage_*``
    variables, so the command is handed them again: a task's run in the turn
    reads the arguments it was given."""

    parse = [f"{name}={value}" for name, value in os.environ.items() if name.startswith("usage_")]
    return ["env", f"HQ_TURN_BOUND={bound}", *IN_TURN, "env", *parse]


def lock_file(name: str) -> Path:
    """The file mise flocks for the task lock ``name``: under its state directory,
    named for the lock's hash (src/task/task_lock.rs, ``LockKey::path``)."""

    state = os.environ.get("MISE_STATE_DIR") or Path(
        os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state", "mise"
    )
    return Path(state, "task-locks", hashlib.sha256(name.encode()).hexdigest()[:16] + ".lock")


def land(command: list[str]) -> int:
    """Run ``command`` with the gate closed to every heavy task outside it, and return its status.

    The command starts at once, its tasks told they hold the gate, and a thread
    takes it beside them. mise polls a busy lock every second at most; this
    blocks in flock(2), which the kernel wakes as the holder lets go, so the
    landing waits behind only the heavy run already in the turn. Its own heavy
    tasks still take turns among themselves on LOCK."""

    path = lock_file(GATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only its owner opens it: mise, run as the same user, opens it read-write to take it too.
    gate = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    record = path.with_suffix(".json")
    threading.Thread(target=hold, args=(gate, record), daemon=True).start()
    held = {*filter(None, os.environ.get(HELD, "").split(",")), GATE}
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
    """Whether this process runs under a task that holds the turn, so a heavy run goes ahead at once."""

    return LOCK in os.environ.get("MISE_TASK_LOCKS", "").split(",")


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


def narrow(argv: list[str]) -> bool:
    """Whether the run manage.py test makes of ``argv`` goes ahead without the heavy turn."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    serial = False
    try:
        django.setup()
        options = vars(TestCommand().create_parser("manage.py", "test").parse_args(argv))
        labels = options.pop("args")
        serial = options["parallel"] == 1
        # A serial run is one process however many tests it loads.
        if not serial and (not labels or any(map(names_a_tree, labels))):
            return False
        # The workers manage.py test asks for (its handle() resolves "auto" so),
        # loaded without a word: the run says what it found.
        workers = get_max_test_processes() if options["parallel"] == "auto" else options["parallel"]
        runner = get_runner(settings, options["testrunner"])(**(options | {"parallel": workers, "verbosity": 0}))
        suite = runner.build_suite(labels)
        # build_suite cuts the workers to the test classes it found, as the run
        # does, so more than one left is a parallel run, heavy however few its labels.
        return runner.parallel <= 1 and not any(map(drives_a_browser, tests_in(suite)))
    except Exception, SystemExit:  # noqa: BLE001 - manage.py says what stopped it, in the turn
        # A serial run that cannot load its tests fails in one process, so it holds nothing heavy.
        return serial


if __name__ == "__main__":
    if sys.argv[1:2] == ["--landing"]:
        sys.exit(land(sys.argv[2:]))
    sys.exit(0 if narrow(sys.argv[1:]) else 1)
