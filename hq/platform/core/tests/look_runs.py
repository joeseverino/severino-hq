"""The folder each look shoots into, named for when the run began, so a later
run never overwrites what an earlier one printed. A run sent to a second
machine (``tools/gate/remote.py``) shoots there into a worktree of its own and
is brought back into this checkout's folder, so the name is reserved here,
where every run of the checkout lands, and handed to the run there as
``RUN``."""

import re
import shutil
import time
from contextlib import suppress
from pathlib import Path

# The name this checkout reserved for a run made elsewhere.
RUN = "HQ_LOOK_RUN"
# The runs from the last `KEPT_FOR` seconds are kept, and at least the newest
# `KEPT_RUNS` whatever their age, and everything else in the folder is removed.
KEPT_RUNS = 5
KEPT_FOR = 24 * 60 * 60
_RUN_NAME = re.compile(r"\d{8}-\d{6}(?:-\d+)?")


def new_run(root: Path, kept: int = KEPT_RUNS, name: str = "") -> Path:
    """A new folder in ``root``, named ``name`` where one was reserved for it
    and else for now, kept with the runs from the last ``KEPT_FOR`` seconds
    and the newest ``kept`` in all, and every other entry of ``root``
    removed. A run's age is read from its name."""

    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    while True:
        runs = sorted(
            (entry for entry in root.iterdir() if _RUN_NAME.fullmatch(entry.name) and entry.is_dir()),
            key=lambda entry: [int(part) for part in entry.name.split("-")],
        )
        if name:
            folder = root / name
            folder.mkdir(exist_ok=True)
            break
        # Past the newest run of this second, so a name pruned this second is never used again.
        newest = runs[-1].name if runs else ""
        again = int(newest.removeprefix(stamp).lstrip("-") or 0) + 1 if newest.startswith(stamp) else 0
        folder = root / (f"{stamp}-{again}" if again else stamp)
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        break
    cutoff = time.time() - KEPT_FOR
    keep = {folder, *(runs[-(kept - 1) :] if kept > 1 else ())}
    # Another run pruning at once may take an entry first, or make a run after the listing, which is recent.
    for entry in root.iterdir():
        if entry in keep or (_RUN_NAME.fullmatch(entry.name) and entry.is_dir() and _began(entry.name) >= cutoff):
            continue
        if entry.is_dir() and not entry.is_symlink():
            with suppress(FileNotFoundError):
                shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)
    return folder


def _began(name: str) -> float:
    return time.mktime(time.strptime(name[:15], "%Y%m%d-%H%M%S"))
