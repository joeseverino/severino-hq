"""Where a test run writes, kept out of the working tree and out of every other run.

Each run gets its own directories, made by the process that starts it and
inherited by parallel workers through the environment, removed when it ends.
An explicit setting still wins.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path


def _directory(variable: str, kind: str) -> str:
    if variable not in os.environ:
        os.environ[variable] = tempfile.mkdtemp(prefix=f"severino-test-{kind}-")
        atexit.register(shutil.rmtree, os.environ[variable], True)
    return os.environ[variable]


def isolate() -> None:
    # Tests upload receipts and write exports as real files.
    _directory("SEVERINO_MEDIA_ROOT", "media")
    # What a test builds to run, such as a controller from this checkout:
    # this run's, so no build outlives it or is mistaken for a later one's.
    _directory("SEVERINO_TEST_BUILDS", "builds")
    _directory("SEVERINO_EXPORTS_ROOT", "exports")
    # The controller's markers otherwise sit beside the test database, in the
    # shared temporary directory, where a heartbeat one run wrote decides
    # whether the next run's controller is arriving: macOS keeps that
    # directory, and a CI runner starts with it empty.
    markers = Path(_directory("SEVERINO_TEST_MARKERS", "markers"))
    for variable, marker in (
        ("SEVERINO_CONTROLLER_HEARTBEAT", "controller-heartbeat"),
        ("SEVERINO_CONTROLLER_DOORBELL", "controller-doorbell"),
        ("SEVERINO_ACTIVITY_MARKER", "hq-activity"),
    ):
        os.environ.setdefault(variable, str(markers / marker))
