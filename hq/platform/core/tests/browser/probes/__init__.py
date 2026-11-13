"""In-page probes: the scripts the browser gates run on a page, apart from the
tests that run them. Each finding probe returns a list of findings; empty is
the pass.

A module declares the survey checks it answers beside their sources, as Django
apps register their system checks: a module-level ``CHECKS`` mapping each
check's name to its probe (a source, or its source and argument, or a function
returning that pair when reading it needs a tool the image lacks), or to a
``Check`` when the check is more than a probe on every page. The survey
(`survey.py`) collects them from every module here, and planted.py holds each
to a plant it must find."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Check:
    """A survey check and where it runs: ``palette`` when its verdict depends
    on the palette the page is drawn in, ``table`` when the sweep of every
    width reads it too, ``scripted`` when it holds only once script has run,
    and ``findings`` what turns its probe's value into findings when that is
    not the value itself."""

    probe: Any
    palette: bool = False
    table: bool = False
    scripted: bool = False
    findings: Callable[[Any], list] | None = None
