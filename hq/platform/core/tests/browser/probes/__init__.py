"""In-page probes: the scripts the browser gates run on a page, apart from the
tests that run them. Each finding probe returns a list of findings; empty is
the pass.

A module declares the survey checks it answers beside their sources, as Django
apps register their system checks: a module-level ``CHECKS`` mapping each
check's name to a ``Check`` of its probe (a source, or its source and
argument, or a function returning that pair when reading it needs a tool the
image lacks). The survey (`survey.py`) collects them from every module here,
and planted.py holds each to a plant it must find.

A check holds only what a reader would see break, the bar docs/Design.md
opens with, and says in ``reader_sees`` what that is."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Check:
    """A survey check, what a reader sees when it fails (``reader_sees``),
    and where it runs: ``palette`` when its verdict depends on the palette the
    page is drawn in, ``table`` when the sweep of every width reads it too,
    ``scripted`` when it holds only once script has run, and ``findings`` what
    turns its probe's value into findings when that is not the value itself."""

    probe: Any
    reader_sees: str = field(kw_only=True)
    palette: bool = False
    table: bool = False
    scripted: bool = False
    findings: Callable[[Any], list] | None = None
