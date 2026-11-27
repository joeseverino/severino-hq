"""In-page probes: the scripts the browser gates run on a page, apart from the
tests that run them. Each finding probe returns a list of findings; empty is
the pass.

A module declares the survey checks it answers beside their sources, as Django
apps register their system checks: a module-level ``CHECKS`` mapping each
check's name to a ``Check`` of its probe (a source, or its source and
argument, or a function returning that pair when reading it needs a tool the
image lacks). The survey (`survey.py`) collects them from every module here.

A check carries its own examples: markup it must find (``plants``, each with
the findings it expects) and markup it must pass (``clear``). One test walks
them all (`plants.py`), so a new check is held by its examples and needs no
test of its own.

A check that measures a shared part names it (``part``), and its probe says
how many of the part's elements it read beside what it found: ``{read,
found}``. The part's own catalog entry draws it, so a probe that reads none
there has measured nothing (`BrowserGate.checked`).

A check holds only what a reader would see break, the bar docs/Design.md
opens with, and says in ``reader_sees`` what that is."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from hq.platform.core.template_parts import Part
from hq.platform.core.tests.browser.widths import WIDE


@dataclass(frozen=True)
class Example:
    """Markup a check reads, alone on a probe page (`reading.probe`, which
    prints ``values``) at ``width``, with script when ``scripted``.
    ``setup`` is a script run on the loaded page for what markup cannot
    say (a popover shown, a modal opened), and ``premise`` a probe and its
    argument returning what the page falls short of to stand for the case
    it is drawn for, as a catalog entry's (`Entry.premise`)."""

    markup: str
    width: int = WIDE
    scripted: bool = False
    values: Mapping[str, Any] = field(default_factory=dict)
    setup: str = ""
    premise: tuple[str, Any] | None = None


@dataclass(frozen=True)
class Plant(Example):
    """Markup its check must find: one finding for each of ``finds``, each
    holding its text, and nothing more, so what the markup holds beside the
    plant is held clear."""

    finds: tuple[str, ...] = field(kw_only=True)


@dataclass(frozen=True)
class Check:
    """A survey check, what a reader sees when it fails (``reader_sees``),
    and where it runs: ``palette`` when its verdict depends on the palette the
    page is drawn in, ``table`` when the sweep of every width reads it too,
    ``scripted`` when it holds only once script has run, ``findings`` what
    turns its probe's value into findings when that is not the value itself,
    ``part`` the shared part it measures, and its examples, ``plants`` it
    must find and ``clear`` markup it must pass."""

    probe: Any
    reader_sees: str = field(kw_only=True)
    palette: bool = False
    table: bool = False
    scripted: bool = False
    findings: Callable[[Any], list] | None = None
    part: Part | None = None
    plants: tuple[Plant, ...] = ()
    clear: tuple[Example, ...] = ()

    def __post_init__(self):
        if not self.reader_sees.strip():
            raise ValueError("a check says what a reader sees when it fails")
