"""A whole-tree walk run in a process of its own, its answer read back as JSON.

Python keeps the memory a parsed tree took for as long as its process lives,
so a walk run in a test worker or the fast gate would hold it through the
rest of that run. Run apart, the walk's memory goes back to the system when
its process exits. The child imports only the walk's module, so a walk run
this way takes JSON arguments and answers JSON. It is a subprocess because a
test worker is a multiprocessing pool's daemon, which multiprocessing allows
no children.
"""

import json
import subprocess
import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]


def apart(walk: Callable[..., Any], *args: Any) -> Any:
    """What ``walk(*args)`` answers, run by ``python -m`` this module; a path argument is passed as its string."""

    done = subprocess.run(
        [sys.executable, "-m", __name__, walk.__module__, walk.__qualname__],
        input=json.dumps(args, default=str),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        raise RuntimeError(f"{walk.__module__}.{walk.__qualname__} failed in its own process:\n{done.stderr}")
    return json.loads(done.stdout)


def main() -> None:
    module, name = sys.argv[1:]
    json.dump(getattr(import_module(module), name)(*json.load(sys.stdin)), sys.stdout)


if __name__ == "__main__":
    main()
