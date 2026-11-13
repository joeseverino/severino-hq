"""What a figure may be drawn as, beside being said.

A ``Kpi`` takes one drawing or none: dots for a part of a whole, a trend for
recent readings, or an ``AllocationBar``, the parts of one whole as one bar.
``hq_sdk.ui`` hands these to extensions.
"""

import math
from typing import ClassVar

from .display_words import display
lazy from .ui import STATUS_VALUES


@display
class Dot:
    """One of the things a figure counts: a machine among the machines.

    Named so it can be pointed at: what it says when hovered, and where it
    leads when it is a thing with a page of its own.
    """

    filled: bool
    tip: str = ""
    url: str = ""


@display
class Dots:
    """A part of a whole, drawn as one dot each beside the value.

    "5 of 5" is then seen rather than read. The words stay in the figure's
    note: dots that only repeat them are decoration and are not announced,
    and dots that lead somewhere are links and are.
    """

    dots: tuple[Dot, ...]
    # Drawn in the order given, as a history is, oldest first; otherwise the
    # filled ones lead, as a part of a whole reads.
    ordered: bool = False

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
        """The dots as drawn: filled first unless ``ordered``; none when there are too many to count."""

        if not 0 < len(self.dots) <= self.LIMIT:
            return ()
        if self.ordered:
            return self.dots
        return tuple(sorted(self.dots, key=lambda dot: not dot.filled))

    @property
    def links(self) -> bool:
        """Whether a dot leads somewhere, so the figure cannot itself be one link."""

        return any(dot.url for dot in self.shown)


@display
class Trend:
    """A figure's recent readings, oldest first, drawn as a line beside it.

    A direction, beside the value or under it where the card is narrow; the
    figure and its note still carry the numbers. ``tips`` says what each reading is when pointed at, in
    the domain's own words (its date, its amount); without them a point names
    its bare number.
    """

    values: tuple[float, ...]
    tips: tuple[str, ...] = ()

    kind: ClassVar[str] = "trend"
    # A line has no part that leads anywhere.
    links: ClassVar[bool] = False
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


@display
class Share:
    """One part of an ``AllocationBar``: what it is, how much, and its color slot.

    ``value`` is the part's own amount; the allocation divides it by the sum
    of every part, so a caller hands over the amounts it already has. A bar
    drawn with its legend shows ``detail`` (the amount as the page says it),
    in ``tone`` when one is given, and links the row to ``url``.
    """

    label: str
    value: float
    slot: int
    url: str = ""
    detail: str = ""
    tone: str = ""

    def __post_init__(self) -> None:
        if self.tone and self.tone not in STATUS_VALUES:
            raise ValueError(
                f"Share tone must be one of {', '.join(sorted(STATUS_VALUES))} or empty; got {self.tone!r}."
            )


@display
class AllocationBar:
    """Parts of one whole as a single bar, each as wide as its share.

    Drawn by the host (``partials/_allocation.html``) from the shares alone,
    so a page writes no width of its own. ``label`` names the whole for a
    screen reader, which hears each part's label and share after it.
    """

    label: str
    parts: tuple[Share, ...]

    kind: ClassVar[str] = "allocation"
    # The narrowest a part with any value is drawn, as a share of the bar, so
    # a sliver still shows; the widest part gives up what that takes.
    FLOOR: ClassVar[float] = 0.5

    def __post_init__(self) -> None:
        if any(part.slot not in range(1, 6) for part in self.parts):
            raise ValueError("AllocationBar slots must be between 1 and 5.")
        if any(part.value < 0 or not math.isfinite(part.value) for part in self.parts):
            raise ValueError("AllocationBar values must be finite and non-negative.")

    @property
    def drawn(self) -> tuple[dict[str, float | int], ...]:
        """Each part with a value, placed: where it starts and how wide it is, in percent."""

        whole = sum(part.value for part in self.parts)
        shown = [part for part in self.parts if part.value > 0]
        if not whole:
            return ()
        widths = [max(self.FLOOR, part.value / whole * 100) for part in shown]
        widest = widths.index(max(widths))
        widths[widest] -= sum(widths) - 100
        drawn, at = [], 0.0
        for part, width in zip(shown, widths, strict=True):
            drawn.append({"slot": part.slot, "at": round(at, 4), "width": round(width, 4)})
            at += width
        return tuple(drawn)

    @property
    def legend(self) -> tuple[tuple[Share, str], ...]:
        """Each part with a value, and its share of the whole as a percent."""

        whole = sum(part.value for part in self.parts)
        return tuple((part, f"{part.value / whole:.0%}") for part in self.parts if part.value > 0)

    @property
    def links(self) -> bool:
        """Whether a part leads somewhere, so a figure drawn with the bar cannot itself be one link."""

        return any(part.url for part, _percent in self.legend)

    @property
    def spoken(self) -> str:
        """The bar as a screen reader hears it: the whole, then each part and its share."""

        heard = ", ".join(f"{part.label} {percent}" for part, percent in self.legend)
        return f"{self.label}: {heard}" if heard else self.label


@display
class StripTile:
    """One of a strip of small figures read across, each as a person scans
    a forecast: what it is of, the figure, and what stands out about it.

    Drawn by ``partials/_tile_strip.html``. ``note`` is said only when there
    is something to say, and then it is marked; ``tip`` is the tile's whole
    sentence, on hover.
    """

    label: str
    value: str
    note: str = ""
    tip: str = ""
