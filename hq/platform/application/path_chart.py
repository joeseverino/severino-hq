"""A drawing in its own coordinate system, rendered by the shared chart frame."""

from dataclasses import dataclass
from typing import ClassVar

from .display_words import display


@dataclass(frozen=True, slots=True)
class PathSegment:
    """One path, with its place in an ordered color scale and its reading."""

    path: str
    band: int
    tooltip: str = ""


@display
class PathMarker:
    """A labeled point in the drawing's coordinate system."""

    x: float
    y: float
    label: str


@display
class PathScale:
    """A length in the drawing and what that length represents."""

    x: float
    y: float
    length: float
    label: str

    @property
    def end(self) -> float:
        return self.x + self.length

    @property
    def label_y(self) -> float:
        return self.y - 6


@display(kw_only=True)
class PathChart:
    """A spatial path with an ordered legend, markers and an optional scale.

    Coordinates are supplied by the caller. Rendering makes no outside request
    and places no location in an address; paths and text are escaped in HTML.
    """

    title: str
    description: str
    segments: tuple[PathSegment, ...]
    width: int
    height: int
    markers: tuple[PathMarker, ...] = ()
    scale: PathScale | None = None
    legend: tuple[tuple[int, str], ...] = ()
    note: str = ""
    spatial: ClassVar[bool] = True

    @property
    def empty(self) -> bool:
        return not self.segments
