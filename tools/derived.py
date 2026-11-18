"""The check-or-write a derived file's writer runs as its lint:drift-* gate
(docs/Development.md, Derived files)."""

import sys
from pathlib import Path

from tests.mise_tasks import ROOT


def hold(path: Path, built: bytes, gate: str, *, fix: bool, wrote: str = "") -> int:
    """Check ``path`` holds ``built``, or write it when ``fix``, the gate's ``--fix``.

    ``wrote`` adds to the line a write prints."""

    name = path.relative_to(ROOT)
    if not fix:
        if not path.exists() or path.read_bytes() != built:
            print(f"{name} is behind its source; run `mise run {gate} --fix` and commit the result.", file=sys.stderr)
            return 1
        print(f"{name} is current.")
        return 0
    path.write_bytes(built)
    print(f"Wrote {name}{wrote}.")
    return 0
