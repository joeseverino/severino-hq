"""Whether a run of manage.py test is narrow enough to go ahead without the
machine's heavy turn (tools/lib/heavy.sh), for every task that runs tests on
labels: `python -m tools.gate.turn [manage.py test arguments...]` exits 0 for a
narrow run and 1 for a heavy one. What narrow means: docs/Development.md, Heavy
gates take turns.
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
    try:
        django.setup()
        options = vars(TestCommand().create_parser("manage.py", "test").parse_args(argv))
        labels = options.pop("args")
        if not labels or any(map(names_a_tree, labels)):
            return False
        # One suite, not a parallel run's partition of it, loaded without a word: the run says what it found.
        runner = get_runner(settings, options["testrunner"])(**(options | {"parallel": 1, "verbosity": 0}))
        return not any(map(drives_a_browser, tests_in(runner.build_suite(labels))))
    except Exception, SystemExit:  # noqa: BLE001 - manage.py says what stopped it, in the turn
        return False


if __name__ == "__main__":
    sys.exit(0 if narrow(sys.argv[1:]) else 1)
