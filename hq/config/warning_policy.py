"""Under test, a warning from this codebase is a failure rather than a line.

A warning printed into a green run is read once and then scrolled past, and the
next one joins it. So when the suite runs, any warning attributed to one of
this repository's own packages is an error, and so is every ``ResourceWarning``
except under ``-X tracemalloc``, which is how a leak is located.
"""

import atexit
import os
import sys
import tempfile
import tracemalloc
import warnings
from pathlib import Path

_LEDGER = "SEVERINO_TEST_LEAK_LEDGER"


def enforce(base_dir: Path) -> None:
    # The repository's packages, read from the tree rather than listed, so a new
    # app is covered the day it is added.
    # Attribution follows the caller a library names with ``stacklevel``, so a
    # deprecated third-party API used from here fails here, and a library's
    # warnings about its own internals do not.
    packages = sorted(path.name for path in base_dir.iterdir() if (path / "__init__.py").is_file())
    warnings.filterwarnings("error", module=rf"({'|'.join(packages)})(\.|$)")
    if tracemalloc.is_tracing():
        # Raised as an error, the warning loses the site that opened the
        # resource, the one fact that finds the leak, since the finalizer runs
        # wherever collection happened to.
        warnings.simplefilter("always", ResourceWarning)
        return
    warnings.simplefilter("error", ResourceWarning)
    # A ResourceWarning comes from a finalizer, where an error cannot propagate
    # (Python hands it to ``sys.unraisablehook``, prints it and carries on), so
    # the hook writes each one down and the process that started the run exits
    # non-zero once it has finished if anything was written. One ledger serves a
    # parallel run, its path inherited by the workers through the environment.
    if _LEDGER not in os.environ:
        # First import is the process that started the run; workers find the
        # path already set and only write to it.
        handle, ledger = tempfile.mkstemp(prefix="severino-leaks-")
        os.close(handle)
        os.environ[_LEDGER] = ledger
        atexit.register(_fail_if_anything_leaked, ledger)
    sys.unraisablehook = _record_leak


# Ending the process at the leak would be wrong: Django's worker pool replaces a
# dead worker without noticing its test never finished, so the run hangs rather
# than failing. Raising into the interrupted test is not possible either; the
# exception fires inside the hook, which Python ignores in turn. A leak collected
# while the interpreter shuts down, after the ledger has been read, has nothing
# left to wait for and does exit.
def _record_leak(unraisable) -> None:
    sys.__unraisablehook__(unraisable)
    if not isinstance(unraisable.exc_value, ResourceWarning):
        return
    if sys.is_finalizing():
        _exit_failed(f"{unraisable.exc_value}\n")
    with Path(os.environ[_LEDGER]).open("a", encoding="utf-8") as ledger:
        ledger.write(f"{unraisable.exc_value}\n")


def _fail_if_anything_leaked(ledger: str) -> None:
    path = Path(ledger)
    leaks = path.read_text(encoding="utf-8")
    path.unlink()
    if leaks:
        _exit_failed(leaks)


def _exit_failed(leaks: str) -> None:
    # Exit, not raise: neither an atexit handler nor a finalizer can change the
    # status the process ends with any other way.
    sys.stdout.flush()
    sys.stderr.write(
        "\nLeaked resources fail the test run (config/warning_policy.py). Each\n"
        "traceback above is where one was collected; rerun with\n"
        "-X tracemalloc=25 to see where it was opened.\n" + leaks
    )
    sys.stderr.flush()
    os._exit(1)
