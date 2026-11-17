"""One figure of HQ's summary band."""

from dataclasses import dataclass
from datetime import datetime

from ..display_words import as_members
from ..tone import Tone


@dataclass(frozen=True, slots=True)
class Tile:
    """One figure of the summary band."""

    label: str
    value: str
    detail: str = ""
    url: str = ""
    tone: Tone = Tone.NEUTRAL
    icon: str = ""

    def __post_init__(self) -> None:
        as_members(self)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
