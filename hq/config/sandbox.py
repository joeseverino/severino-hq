"""Where a test run writes, kept out of the working tree and out of every other run.

Each run gets its own directories, made by the process that starts it and
inherited by parallel workers through the environment, removed when it ends.
An explicit setting still wins.

Every path a run makes in the temporary directory is named for the process
that started it, so the files of a run that was killed, and never reached its
cleanup, can be told from those of a run still going.
"""

import atexit
import contextlib
import os
import re
import shutil
import tempfile
from pathlib import Path

PREFIX = "severino-test-"
# severino-test-<pid>.sqlite3, its clones (_N), sidecars (-wal, -shm), and
# severino-test-<kind>-<pid>-<random> directories.
_OWNED = re.compile(rf"{re.escape(PREFIX)}(?:[a-z]+-)?(\d+)[._-]")


def database_path() -> str:
    return str(Path(tempfile.gettempdir()) / f"{PREFIX}{os.getpid()}.sqlite3")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def sweep_abandoned(directory: Path, pid: int) -> list[Path]:
    """Delete what runs whose process is gone left in ``directory``."""

    removed = []
    for candidate in directory.glob(f"{PREFIX}*"):
        match = _OWNED.match(candidate.name)
        if not match or int(match[1]) == pid or _alive(int(match[1])):
            continue
        if candidate.is_dir():
            shutil.rmtree(candidate, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                candidate.unlink()
        removed.append(candidate)
    return removed


def _directory(variable: str, kind: str) -> str:
    if variable not in os.environ:
        os.environ[variable] = tempfile.mkdtemp(prefix=f"{PREFIX}{kind}-{os.getpid()}-")
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
