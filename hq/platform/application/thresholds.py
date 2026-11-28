"""A bar a finding's detector compares what it read against.

Declared once beside its rule, so the detector compares against its value and
the rule's bars and clears say it in its own words, and the two cannot part.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from .labels import human_bytes
from .moments import Span
from .ui import measured

# What its value counts, which says how it is said.
type Measure = Literal["days", "bytes", "share", "time"]


@dataclass(frozen=True, slots=True)
class Threshold[V: (int, float, timedelta)]:
    """A value and what it counts, said as a bar says it: "7 days", "512 MB",
    "3%", "1 hour"."""

    value: V
    measure: Measure

    def __post_init__(self) -> None:
        if (self.measure == "time") != isinstance(self.value, timedelta):
            raise ValueError(f"Threshold {self.value!r}: a length of time is a timedelta, and only it.")

    def __str__(self) -> str:
        value = self.value
        if isinstance(value, timedelta):
            return Span(value)
        if self.measure == "bytes":
            return human_bytes(value)
        if self.measure == "share":
            return measured(value * 100, "%", 0)
        return Span(timedelta(days=value))
