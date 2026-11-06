"""Whether a run of manage.py test is narrow enough to go ahead without the
machine's heavy turn (mise's task lock `heavy`), for every task that runs tests
on labels: `python -m tools.gate.turn [manage.py test arguments...]` exits 0 for
a narrow run and 1 for a heavy one. What narrow means: docs/Development.md,
Heavy gates take turns.
"""

import importlib.util
import os
import sys
from pathlib import Path

import django
from django.conf import settings
from django.core.management.commands.test import Command as TestCommand
from django.test.utils import get_runner

from hq.platform.core.test_runner import drives_a_browser, tests_in

# The lock every heavy task takes, and the task that runs a command in it for
# a task that weighs its own run (mise-tasks/turn).
LOCK = "heavy"
IN_TURN = ["mise", "run", "turn", "--"]


def in_turn(bound: str) -> list[str]:
    """The command that runs what follows it in the turn, stopped once it has held the turn for ``bound`` (30m, 2h).

    mise gives the turn task its own parse, dropping the caller's ``usage_*``
    variables, so the command is handed them again: a task's run in the turn
    reads the arguments it was given."""

    parse = [f"{name}={value}" for name, value in os.environ.items() if name.startswith("usage_")]
    return ["env", f"HQ_TURN_BOUND={bound}", *IN_TURN, "env", *parse]


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
        # One suite, not a parallel run's partition of it, loaded without a word: the run says what it found.
        runner = get_runner(settings, options["testrunner"])(**(options | {"parallel": 1, "verbosity": 0}))
        return not any(map(drives_a_browser, tests_in(runner.build_suite(labels))))
    except Exception, SystemExit:  # noqa: BLE001 - manage.py says what stopped it, in the turn
        # A serial run that cannot load its tests fails in one process, so it holds nothing heavy.
        return serial


if __name__ == "__main__":
    sys.exit(0 if narrow(sys.argv[1:]) else 1)
