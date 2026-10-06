"""The "did I do this" strip: whether, period after period, and what it adds up to.

``application.ui`` exports these beside the charts; they are kept here so the
rule of a period with no data (drawn as its own mark, counted in no total) is
in one short module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CadenceWeek:
    """One period in a "did I do this" strip.

    A bar says how much. A mark says whether it happened that period, so a
    missed one shows as a gap in the row.
    """

    label: str
    hit: bool
    # How many times in the period, when more than once is meaningful.
    count: int = 0
    # Read out for assistive technology, and shown on hover.
    detail: str = ""
    # Nothing is known about the period, which is a different fact from
    # nothing having been done in it. Drawn as its own mark and left out of
    # every total.
    no_data: bool = False

    def __post_init__(self) -> None:
        if self.no_data and (self.hit or self.count):
            raise ValueError(
                f"{self.label!r} cannot both have no data and have been done."
            )

    @property
    def said(self) -> str:
        """What the mark says when pointed at or read out."""
        return self.detail or (NO_DATA if self.no_data else "")


# What a period nothing is known about says when its owner gave no detail.
NO_DATA = "No data"


def _known(weeks: tuple[CadenceWeek, ...]) -> tuple[CadenceWeek, ...]:
    """The periods that have data: the only ones a total counts."""
    return tuple(week for week in weeks if not week.no_data)


def _hits(weeks: tuple[CadenceWeek, ...]) -> int:
    return sum(1 for week in _known(weeks) if week.hit)


def _streak(weeks: tuple[CadenceWeek, ...]) -> int:
    """Consecutive periods with a hit, counting back from the most recent with data."""

    run = 0
    for week in reversed(_known(weeks)):
        if not week.hit:
            break
        run += 1
    return run


def _tally(weeks: tuple[CadenceWeek, ...]) -> str:
    """Hits out of the periods with data, as "n/m"; MISSING when none has any."""
    from .ui import MISSING

    known = len(_known(weeks))
    return f"{_hits(weeks)}/{known}" if known else MISSING


@dataclass(frozen=True)
class Cadence:
    title: str
    description: str
    weeks: tuple[CadenceWeek, ...]

    @property
    def hits(self) -> int:
        return _hits(self.weeks)

    @property
    def tally(self) -> str:
        return _tally(self.weeks)

    @property
    def streak(self) -> int:
        """Consecutive periods, counting back from the most recent with data."""
        return _streak(self.weeks)


@dataclass(frozen=True)
class CadenceRow:
    """One thing tracked across the shared periods of a matrix."""

    label: str
    weeks: tuple[CadenceWeek, ...]
    url: str = ""
    detail: str = ""

    @property
    def hits(self) -> int:
        return _hits(self.weeks)

    @property
    def tally(self) -> str:
        return _tally(self.weeks)

    @property
    def streak(self) -> int:
        return _streak(self.weeks)


@dataclass(frozen=True)
class CadenceMatrix:
    """Several cadences sharing one set of periods, so they can be compared.

    Separate strips answer "did I keep this up" one at a time; stacked in
    columns that line up they answer "which of these am I neglecting", which is
    the question worth asking when there is more than one. The period labels
    appear once, at the top, because that alignment is the whole point.
    """

    periods: tuple[str, ...]
    rows: tuple[CadenceRow, ...]

    def __post_init__(self) -> None:
        for row in self.rows:
            if len(row.weeks) != len(self.periods):
                raise ValueError(
                    f"{row.label!r} has {len(row.weeks)} periods; the matrix "
                    f"has {len(self.periods)}. Columns that do not line up "
                    "make the comparison wrong rather than merely ugly."
                )
