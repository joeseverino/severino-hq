"""The tones a part of HQ's page is said in, and one figure of its summary band."""

from dataclasses import dataclass
from datetime import datetime

GOOD = "good"
ATTENTION = "attention"
SERIOUS = "serious"
NEUTRAL = "neutral"


@dataclass(frozen=True, slots=True)
class Tile:
    """One figure of the summary band."""

    label: str
    value: str
    detail: str = ""
    url: str = ""
    tone: str = NEUTRAL
    icon: str = ""


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
