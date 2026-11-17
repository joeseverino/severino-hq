"""A look of many pages split across processes, each with its own browser and
test database, so a survey of every page uses the cores the heavy turn gives
it rather than one.

The pages are dealt as CI deals a browser gate's sweep among its jobs
(``browser/shards.py``'s ``share``): every n-th page to each of n shares. A
share is told its own as ``HQ_LOOK_SHARE``, never as CI's
``HQ_BROWSER_SHARD``, under which the test runner would leave out the look's
one test in every share its id does not pick. The run that was asked looks at the first
share itself and starts the same test run once for each other share; each
writes what it found into the run's folder, and the first merges them in the
order the pages were asked, so the report reads as one process's. Django's
runner splits a run by test class and Playwright's ``--shard`` belongs to its
own runner, while a look is one test over many pages.

How many processes run is the share of the heavy turn the run holds
(``tools/gate/turn.py``), a browser's tokens (``BROWSER``) for each, and never
more than one for each ``PAGES_EACH`` pages, since a process spends its first
seconds building what the first already built.
"""

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from django.db import connections

from hq.platform.core.file_clones import checkpoint
from hq.platform.core.tests.browser.shards import VARIABLE, shard, share as dealt
from hq.platform.core.tests.look_report import Found, Measured

# dev:look is development code like the turn it runs in, and imports it only
# as it decides its processes: the share a look holds is the heavy turn's.
# ast-grep-ignore: tools-imported-only-by-tools
lazy from tools.gate import turn

# The folder a share's process writes into: the one the run that started it shoots into.
FOLDER = "HQ_LOOK_SHARES"
# Which share a process looks at, as i/n.
SHARE = "HQ_LOOK_SHARE"
# The id the pages are dealt by, so every process deals them alike.
DEALT_BY = "dev:look"
# The fewest pages worth a process of their own.
PAGES_EACH = 20

type Key = tuple[str, int]


def processes(pages: int, tokens: int) -> int:
    """How many processes look at ``pages`` pages in a share of ``tokens``."""

    return max(1, min(tokens // turn.BROWSER, pages // PAGES_EACH))


def tokens(processes: int) -> int:
    """The tokens of the heavy turn ``processes`` take: a browser's each."""

    return turn.BROWSER * processes


def capacity() -> int:
    """Every token the machine holds."""

    return turn.CAPACITY


def granted() -> int:
    """The tokens this run holds: its share's, every token when it holds the
    whole turn, or one browser's when it holds neither."""

    if held := os.environ.get(turn.SHARE):
        return int(held)
    return turn.CAPACITY if turn.LOCK in turn.held_locks() else turn.BROWSER


def this_share() -> tuple[int, int] | None:
    """This process's share as ``(i, n)`` when the run that was asked started
    it for one, else None."""

    return shard({VARIABLE: given}) if (given := os.environ.get(SHARE)) else None


def folder() -> Path | None:
    """The folder of the run that started this share, when it did."""

    return Path(given) if (given := os.environ.get(FOLDER)) else None


def mine(pages: list[str], at: tuple[int, int]) -> list[str]:
    """The pages of share ``at``, in the order they were asked."""

    return dealt(pages, DEALT_BY, at)


def said_by(index: int, total: int) -> str:
    """What a share's live lines begin with: no ``[name]`` a mise task's
    output begins with, which a gate's verdict reads as a task's."""

    return f"share {index}/{total}: "


def record(into: Path, index: int) -> Path:
    return into / f"share-{index}.json"


def log(into: Path, index: int) -> Path:
    return into / f"share-{index}.log"


@dataclass
class Looked:
    """What one process looked at: each page and width's findings, its
    measures, the errors it heard, whether its rule ran to the end, and what it
    printed there; and each page that answered with something else."""

    found: Found = field(default_factory=dict)
    measured: Measured = field(default_factory=dict)
    errors: Found = field(default_factory=dict)
    judged: list[Key] = field(default_factory=list)
    printed: dict[Key, list[str]] = field(default_factory=dict)
    unanswered: dict[str, int | None] = field(default_factory=dict)

    def write(self, path: Path) -> None:
        pages = [
            {
                "page": name,
                "width": width,
                "found": said,
                "measured": self.measured.get((name, width), {}),
                "errors": self.errors.get((name, width), []),
                "judged": (name, width) in self.judged,
                "printed": self.printed.get((name, width), []),
            }
            for (name, width), said in self.found.items()
        ]
        path.write_text(json.dumps({"pages": pages, "unanswered": self.unanswered}), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Looked:
        written = json.loads(path.read_text(encoding="utf-8"))
        looked = cls(unanswered=written["unanswered"])
        for entry in written["pages"]:
            key = (entry["page"], entry["width"])
            looked.found[key] = entry["found"]
            looked.measured[key] = entry["measured"]
            looked.errors[key] = entry["errors"]
            looked.printed[key] = entry["printed"]
            if entry["judged"]:
                looked.judged.append(key)
        return looked


def merged(pages: list[str], shares: list[Looked]) -> tuple[Looked, list[str]]:
    """The shares' looks as one, page by page in the order ``pages`` were
    asked and each page's widths narrowest first; and what keeps it from
    being whole: a page no share looked at, or a page and width more than one
    did. A page's widths are its share's to choose (a wide table's only where
    it draws one), so a share that ended part way says so itself."""

    whole, wrong = Looked(), []
    held: dict[Key, Looked] = {}
    for looked in shares:
        for key in looked.found:
            if key in held:
                wrong.append(f"{key[0]} at {key[1]} was looked at by more than one share")
            held[key] = looked
        whole.unanswered |= looked.unanswered
    for name in pages:
        keys = sorted((key for key in held if key[0] == name), key=lambda key: key[1])
        if not keys:
            wrong.append(f"{name} was looked at by no share")
        for key in keys:
            looked = held[key]
            whole.found[key] = looked.found[key]
            whole.errors[key] = looked.errors.get(key, [])
            whole.printed[key] = looked.printed.get(key, [])
            if measured := looked.measured.get(key):
                whole.measured[key] = measured
            if key in looked.judged:
                whole.judged.append(key)
    return whole, wrong


def database_for(index: int) -> Path:
    """A copy of the test database as it stands, for share ``index``: Django's
    clone of it, made as its parallel runner makes one, its log folded in
    first (``file_clones``)."""

    connection = connections["default"]
    checkpoint(connection.alias)
    suffix = f"share{index}"
    connection.creation.clone_test_db(suffix=suffix, verbosity=0, keepdb=False)
    return Path(connection.creation.get_test_db_clone_settings(suffix)["NAME"])


@dataclass
class Started:
    """A share's process, the thread saying its lines, and its database."""

    index: int
    process: subprocess.Popen[str]
    relay: threading.Thread
    database: Path


def say(printed: dict[Key, list[str]]) -> None:
    """Every page's lines, page by page."""

    for lines in printed.values():
        if lines:
            print(*lines, sep="\n", flush=True)


def _relayed(process: subprocess.Popen[str], prefix: str, kept: Path, out: TextIO) -> None:
    with kept.open("w", encoding="utf-8") as sink:
        for line in process.stdout or ():
            sink.write(line)
            sink.flush()
            out.write(f"{prefix}{line}")
            out.flush()


def environment(env: dict[str, str], at: tuple[int, int], into: Path, database: Path) -> dict[str, str]:
    """``env`` for share ``at``: its share, its folder and its database, and no
    share of CI's, under which its runner would leave out the look."""

    return {name: value for name, value in env.items() if name != VARIABLE} | {
        SHARE: f"{at[0]}/{at[1]}",
        FOLDER: str(into),
        "SEVERINO_TEST_DATABASE_PATH": str(database),
        "PYTHONUNBUFFERED": "1",
    }


@contextmanager
def started(total: int, into: Path, command: list[str], env: dict[str, str], out: TextIO) -> Iterator[list[Started]]:
    """Shares 2 to ``total`` running ``command``, each in ``env`` with its share,
    its folder and a database of its own; each line one says is kept in its
    log and said on ``out`` after its share. Each process still running as the
    block ends is stopped, and every copy of the database removed."""

    runs: list[Started] = []
    try:
        for index in range(2, total + 1):
            database = database_for(index)
            process = subprocess.Popen(
                command,
                env=environment(env, (index, total), into, database),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            relay = threading.Thread(
                target=_relayed, args=(process, said_by(index, total), log(into, index), out), daemon=True
            )
            relay.start()
            runs.append(Started(index, process, relay, database))
        yield runs
    finally:
        for run in runs:
            if run.process.poll() is None:
                run.process.kill()
            run.process.wait()
            run.relay.join()
            for path in (run.database, *(run.database.with_name(run.database.name + end) for end in ("-wal", "-shm"))):
                path.unlink(missing_ok=True)


def collected(
    runs: list[Started], into: Path, pages: list[str], why: Callable[[str], str]
) -> tuple[list[Looked], list[str]]:
    """What each share wrote once its process ended, and for each that wrote
    nothing, a line naming its pages and ``why`` its log says it ended."""

    shares, failed, total = [], [], len(runs) + 1
    for run in runs:
        run.process.wait()
        run.relay.join()
        written = record(into, run.index)
        if written.is_file():
            shares.append(Looked.read(written))
            continue
        said = ""
        with suppress(OSError):
            said = log(into, run.index).read_text(encoding="utf-8")
        failed.append(
            f"share {run.index}/{total} ended without its findings ({why(said)}),"
            f" so these were not looked at: {', '.join(mine(pages, (run.index, total)))}"
        )
    return shares, failed


def command(argv: list[str]) -> list[str]:
    """The test run that started this one, run again for a share, on the
    database it is handed as it stands."""

    return [sys.executable, *argv, *([] if "--keepdb" in argv else ["--keepdb"])]
