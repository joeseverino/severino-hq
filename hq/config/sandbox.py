"""Where a test run or a gate writes, kept out of the working tree and out of every other run.

Each run gets a temporary directory of its own, as Bazel gives each test its
TEST_TMPDIR: made by the process that starts the run, named to every process it
starts (parallel workers, the browser's driver, a controller it builds) as their
TMPDIR, and removed when it ends, so whatever any test makes there goes with it.
A gate's process that names no database (``SEVERINO_SANDBOX``, which mise.toml's
django template sets) is run the same way, its database a new file in that
directory. An explicit setting still wins.
"""

import contextlib
import fcntl
import os
import shutil
import tempfile
import time
from pathlib import Path
lazy from multiprocessing.util import Finalize

# Short, since a socket a test binds inside a run's directory must fit the 104
# bytes macOS gives a socket's path.
RUN_PREFIX = "hq-run-"
RUN = "SEVERINO_TEST_RUN"
DATABASE = "SEVERINO_DATABASE_PATH"
# A file in a run's directory that the process that started the run holds
# locked, with its pid in it, as pytest's numbered directories hold a .lock.
# The kernel lets go of the lock however the process ends, so a killed run's
# directory is told from a live one's whatever process has its pid now. The
# lock and the pid both outlast the process's exec of itself, which still owns
# the run.
OWNER = ".owner"
# What has no owner that ever held it (the composed suite's copy of the tree,
# which a shell makes) is abandoned once older than any run, as pytest takes a
# lock that old for dead.
LONGEST_RUN = 24 * 60 * 60


def database_path() -> str:
    # A nested test run shares its parent's directory; the pid keeps their databases apart.
    return str(Path(tempfile.gettempdir()) / f"severino-test-{os.getpid()}.sqlite3")


def shared_tempdir() -> Path:
    """The temporary directory the run was started in, for what outlives it (Hypothesis's examples)."""

    return Path(os.environ[RUN]).parent if RUN in os.environ else Path(tempfile.gettempdir())


def _abandoned(candidate: Path) -> bool:
    """Whether ``candidate`` is the directory of a run that has gone, which the next run to start deletes."""

    with contextlib.suppress(OSError):
        handle = os.open(candidate / OWNER, os.O_RDONLY)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            # Empty, its owner has not locked it yet.
            if os.read(handle, 1):
                return True
        finally:
            os.close(handle)
    try:
        return time.time() - candidate.lstat().st_mtime > LONGEST_RUN
    except FileNotFoundError:
        return False


class _Owner:
    """The descriptor this process holds OWNER locked through, for the run it started.

    It is never closed: it stays open for the process's life and across an
    exec of itself, since closing it lets the lock go.
    """

    held: int | None = None


def _run_directory() -> Path:
    """This run's temporary directory, made and locked in the process that starts the run."""

    if RUN not in os.environ:
        for candidate in Path(tempfile.gettempdir()).glob(f"{RUN_PREFIX}*"):
            if _abandoned(candidate):
                shutil.rmtree(candidate, ignore_errors=True)
        run = Path(tempfile.mkdtemp(prefix=RUN_PREFIX))
        owner = os.open(run / OWNER, os.O_RDWR | os.O_CREAT, 0o600)
        _Owner.held = owner
        os.set_inheritable(owner, True)
        fcntl.flock(owner, fcntl.LOCK_EX)
        os.write(owner, str(os.getpid()).encode())
        os.environ.update({RUN: str(run), "TMPDIR": str(run)})
    run = Path(os.environ[RUN])
    with contextlib.suppress(OSError):
        if (run / OWNER).read_text() == str(os.getpid()):
            # multiprocessing removes its own directory (pymp-*, the
            # forkserver's) inside this one in an exit finalizer at priority
            # -100, which runs after any atexit hook made here, so this one
            # is a finalizer of its own that runs after it.
            Finalize(None, shutil.rmtree, args=(run,), kwargs={"ignore_errors": True}, exitpriority=-101)
    tempfile.tempdir = str(run)
    return run


def _directory(variable: str, kind: str, run: Path) -> str:
    if variable not in os.environ:
        (run / kind).mkdir(exist_ok=True)
        os.environ[variable] = str(run / kind)
    return os.environ[variable]


# The controller's markers, each by the setting that moves it. The test
# runner gives each process and each test its own (``isolation_testing.own_markers``).
MARKERS = (
    ("SEVERINO_CONTROLLER_HEARTBEAT", "controller-heartbeat"),
    ("SEVERINO_CONTROLLER_DOORBELL", "controller-doorbell"),
    ("SEVERINO_ACTIVITY_MARKER", "hq-activity"),
)


def isolate(database: str = "") -> str:
    """The database to open: ``database``, or a new empty file in the run's directory,
    named to every process the run starts so they all open the one file."""

    run = _run_directory()
    # Tests upload receipts and write exports as real files.
    _directory("SEVERINO_MEDIA_ROOT", "media", run)
    # What a test builds to run, such as a controller from this checkout:
    # this run's, so no build outlives it or is mistaken for a later one's.
    _directory("SEVERINO_TEST_BUILDS", "builds", run)
    _directory("SEVERINO_EXPORTS_ROOT", "exports", run)
    # The controller's markers otherwise sit beside the test database, where a
    # heartbeat one run wrote would decide whether the next run's controller
    # is arriving.
    markers = Path(_directory("SEVERINO_TEST_MARKERS", "markers", run))
    for variable, marker in MARKERS:
        os.environ.setdefault(variable, str(markers / marker))
    # A repository a test makes gets none of git's template (sample hooks, a
    # description, info/exclude), which no test reads: an empty template
    # directory is git's own way to copy nothing (git-init, TEMPLATE DIRECTORY).
    os.environ.setdefault("GIT_TEMPLATE_DIR", "")
    if not database:
        handle, database = tempfile.mkstemp(prefix="db-", suffix=".sqlite3", dir=run)
        os.close(handle)
        os.environ[DATABASE] = database
    return database
