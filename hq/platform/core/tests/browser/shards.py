"""A browser gate's share of its tests, when CI runs it as several jobs side by side.

A job is told its share as ``HQ_BROWSER_SHARD=i/n``; with none set it is the
whole gate. A sweep (``@sweep``) runs in every share and keeps its share of
the pages it reads (``share``); every other test runs in one share, chosen by
its id (``runs_here``). Either way each page and test lands in exactly one of
the n shares, so the shares together run what the whole gate runs.

GitHub's matrix has no sharding of its own and Django's runner none either;
Playwright's ``--shard`` belongs to its own test runner, which HQ does not use.
"""

import os
import re
import zlib
from collections.abc import Callable, Iterable, Mapping
from typing import Any

VARIABLE = "HQ_BROWSER_SHARD"


def shard(environ: Mapping[str, str] = os.environ) -> tuple[int, int]:
    """This job's share as ``(i, n)``, from 1: ``(1, 1)`` when none is set."""

    said = environ.get(VARIABLE, "")
    if not said:
        return 1, 1
    found = re.fullmatch(r"([1-9]\d*)/([1-9]\d*)", said)
    if not found or int(found[1]) > int(found[2]):
        raise ValueError(f"{VARIABLE} must be i/n with 1 <= i <= n, not {said!r}")
    return int(found[1]), int(found[2])


def _turn(test_id: str) -> int:
    return zlib.crc32(test_id.encode())


def runs_here(test_id: str, at: tuple[int, int] | None = None) -> bool:
    """Whether the test ``test_id`` names runs in this share."""

    index, total = at or shard()
    return _turn(test_id) % total == index - 1


def share[T](items: Iterable[T], test_id: str, at: tuple[int, int] | None = None) -> list[T]:
    """This share's items of a sweep: every n-th, starting where the sweep's
    id turns, so the shares of several short sweeps do not all start in the
    first share."""

    index, total = at or shard()
    start = (index - 1 - _turn(test_id)) % total
    return list(items)[start::total]


def sweep[F: Callable[..., Any]](test: F) -> F:
    """Marks a test that reads its pages through ``BrowserGate.mine``: it runs
    in every share, each reading its own share of the pages."""

    vars(test)["sweeps"] = True
    return test


def sweeps(test: Any) -> bool:
    return getattr(getattr(test, getattr(test, "_testMethodName", ""), None), "sweeps", False)
