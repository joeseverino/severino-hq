"""Under test, a warning from this codebase is a failure rather than a line.

A warning printed into a green run is read once and then scrolled past, and the
next one joins it: an unclosed receipt file, an HTTP error response holding its
socket, a test client on a deprecated path all sat in the output of passing
runs. So when the suite runs, two kinds are made errors.

Any warning attributed to one of this repository's own packages, whatever its
category. Attribution follows the caller a library names with ``stacklevel``,
so a deprecated third-party API used from here fails here, and a library's
warnings about its own internals do not.

Every ``ResourceWarning``, wherever it surfaces. Those come from a finalizer,
and an error raised there cannot propagate -- Python hands it to
``sys.unraisablehook``, prints it and carries on -- so the hook installed here
also writes each one down, and the process that started the run exits non-zero
once it has finished if anything was written. One ledger file serves a parallel
run, because the workers inherit its path through the environment.

Ending the process at the leak would be simpler and is wrong: Django's worker
pool replaces a dead worker without noticing its test never finished, so the
run hangs rather than failing. Raising into the test that was interrupted is
not possible from a hook either; the exception fires inside the hook, which
Python ignores in turn. A leak collected while the interpreter shuts down,
after the ledger has been read, has nothing left to wait for and does exit.

The exception is a run under ``-X tracemalloc``, which is how a leak is located:
there, leaks are reported with the site that opened them instead. Raised as an
error, the warning loses that site, and it is the one fact that finds the leak,
since the finalizer runs wherever collection happened to.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import sys
import tempfile
import tracemalloc
import warnings

_LEDGER = "SEVERINO_TEST_LEAK_LEDGER"


def enforce(base_dir: Path) -> None:
    # The repository's packages, read from the tree rather than listed, so a new
    # app is covered the day it is added.
    packages = sorted(
        path.name for path in base_dir.iterdir() if (path / "__init__.py").is_file()
    )
    warnings.filterwarnings("error", module=rf"({'|'.join(packages)})(\.|$)")
    if tracemalloc.is_tracing():
        warnings.simplefilter("always", ResourceWarning)
        return
    warnings.simplefilter("error", ResourceWarning)
    if _LEDGER not in os.environ:
        # First import is the process that started the run; workers find the
        # path already set and only write to it.
        handle, ledger = tempfile.mkstemp(prefix="severino-leaks-")
        os.close(handle)
        os.environ[_LEDGER] = ledger
        atexit.register(_fail_if_anything_leaked, ledger)
    sys.unraisablehook = _record_leak


def _record_leak(unraisable) -> None:
    sys.__unraisablehook__(unraisable)
    if not isinstance(unraisable.exc_value, ResourceWarning):
        return
    if sys.is_finalizing():
        _exit_failed(f"{unraisable.exc_value}\n")
    with open(os.environ[_LEDGER], "a", encoding="utf-8") as ledger:
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
