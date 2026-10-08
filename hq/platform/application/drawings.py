"""What a figure may be drawn as, beside being said.

A ``Kpi`` takes one drawing or none: dots for a part of a whole, a trend for
recent readings. ``hq_sdk.ui`` hands these to extensions.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class Dot:
    """One of the things a figure counts: a machine among the machines.

    Named so it can be pointed at: what it says when hovered, and where it
    leads when it is a thing with a page of its own.
    """

    filled: bool
    tip: str = ""
    url: str = ""


@dataclass(frozen=True, slots=True)
class Dots:
    """A part of a whole, drawn as one dot each beside the value.

    "5 of 5" is then seen rather than read. The words stay in the figure's
    note: dots that only repeat them are decoration and are not announced,
    and dots that lead somewhere are links and are.
    """

    dots: tuple[Dot, ...]

    kind: ClassVar[str] = "dots"
    # More dots than this is a texture, not a count.
    LIMIT: ClassVar[int] = 12

    @classmethod
    def of(cls, filled: int, whole: int) -> Dots:
        """``filled`` of ``whole``, when the parts are a count and nothing more."""

        if not 0 <= filled <= whole:
            raise ValueError("Dots.of needs 0 <= filled <= whole.")
        return cls(tuple(Dot(filled=index < filled) for index in range(whole)))

    @property
    def shown(self) -> tuple[Dot, ...]:
        """The dots as drawn: filled first; none when there are too many to count."""

        if not 0 < len(self.dots) <= self.LIMIT:
            return ()
        return tuple(sorted(self.dots, key=lambda dot: not dot.filled))

    @property
    def links(self) -> bool:
        """Whether a dot leads somewhere, so the figure cannot itself be one link."""

        return any(dot.url for dot in self.shown)


@dataclass(frozen=True, slots=True)
class Trend:
    """A figure's recent readings, oldest first, drawn as a line beside it.

    A direction, shown where the card has room; the figure and its note still
    carry the numbers. ``tips`` says what each reading is when pointed at, in
    the domain's own words (its date, its amount); without them a point names
    its bare number.
    """

    values: tuple[float, ...]
    tips: tuple[str, ...] = ()

    kind: ClassVar[str] = "trend"
    # The line's box, in the units the template's viewBox is drawn in.
    WIDTH: ClassVar[int] = 100
    HEIGHT: ClassVar[int] = 24
    INSET: ClassVar[int] = 2

    def __post_init__(self) -> None:
        if self.tips and len(self.tips) != len(self.values):
            raise ValueError("Trend tips must name every reading in values.")

    @property
    def marks(self) -> tuple[dict[str, str | bool], ...]:
        """Each reading placed in the line's box, with the strip that answers for it.

        A point on a line this small is too fine to aim at, so a reading
        answers anywhere in the strip of the box nearest to it. Fewer than two
        readings is no line: one point drawn as a chart claims a direction
        nobody observed.
        """

        if len(self.values) < 2:
            return ()
        low, high = min(self.values), max(self.values)
        step = self.WIDTH / (len(self.values) - 1)
        reach = self.HEIGHT - 2 * self.INSET
        marks = []
        for index, value in enumerate(self.values):
            x = index * step
            left = max(0.0, x - step / 2)
            # A flat series sits on the middle of the box rather than its floor.
            level = (value - low) / (high - low) if high > low else 0.5
            marks.append(
                {
                    "x": f"{x:.1f}",
                    "y": f"{self.INSET + reach * (1 - level):.1f}",
                    "left": f"{left:.1f}",
                    "width": f"{min(self.WIDTH, x + step / 2) - left:.1f}",
                    "tip": self.tips[index] if self.tips else f"{value:g}",
                    "latest": index == len(self.values) - 1,
                }
            )
        return tuple(marks)

    @property
    def points(self) -> str:
        """The line as SVG polyline points; empty when there is no line to draw."""

        return " ".join(f"{mark['x']},{mark['y']}" for mark in self.marks)
