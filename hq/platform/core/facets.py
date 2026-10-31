"""Optional, typed things an audit event can carry.

The unit is the fact, not the event: small typed pieces that compose
(``facets=[Counts(created=12), Timing(duration_ms=900)]``), so ``counts.created``
means the same everywhere, which neither the shapeless ``metadata`` column nor
a schema per event kind gives. An unset field is absent from the row, not null.
"""

from dataclasses import dataclass, fields
from typing import ClassVar, override


class Facet:
    """One typed, optional piece of an audit event.

    Subclasses are frozen dataclasses with a `key`. Values are coerced to
    JSON-safe types: a `Decimal` reaching the column fails the write, and
    `record_event` swallows that, losing the event silently.
    """

    key: ClassVar[str] = ""

    def as_metadata(self) -> dict:
        data = {}
        for field in fields(self):  # type: ignore[arg-type]  # every facet subclass is a dataclass
            value = getattr(self, field.name)
            if value is None or value == ():
                continue
            data[field.name] = _plain(value)
        return data


def _plain(value):
    """A JSON-safe version of the value."""
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return str(value)


def _non_negative(instance, *names) -> None:
    for name in names:
        value = getattr(instance, name)
        if value is not None and value < 0:
            raise ValueError(f"{type(instance).__name__}.{name} cannot be negative.")


@dataclass(frozen=True, slots=True)
class Counts(Facet):
    """How much a thing changed."""

    key: ClassVar[str] = "counts"

    seen: int | None = None
    created: int | None = None
    updated: int | None = None
    deleted: int | None = None
    skipped: int | None = None

    def __post_init__(self):
        _non_negative(self, "seen", "created", "updated", "deleted", "skipped")

    @property
    def touched(self) -> int:
        """Rows changed, as opposed to rows read."""
        return sum(value or 0 for value in (self.created, self.updated, self.deleted))


@dataclass(frozen=True, slots=True)
class Timing(Facet):
    """How long it took.

    Milliseconds, not a formatted string: "1m 39s" cannot be compared with
    the run before it.
    """

    key: ClassVar[str] = "timing"

    duration_ms: int | None = None
    queued_ms: int | None = None

    def __post_init__(self):
        _non_negative(self, "duration_ms", "queued_ms")

    @property
    def seconds(self) -> float | None:
        return None if self.duration_ms is None else self.duration_ms / 1000


@dataclass(frozen=True, slots=True)
class Source(Facet):
    """What the work read, so a result can be traced back to its input."""

    key: ClassVar[str] = "source"

    name: str = ""
    bytes: int | None = None
    sha256: str = ""

    def __post_init__(self):
        _non_negative(self, "bytes")


@dataclass(frozen=True, slots=True)
class Steps(Facet):
    """The steps that completed, in order.

    "It failed" does not say which part did. The failure is the step after
    the last one named here.
    """

    key: ClassVar[str] = "steps"

    done: tuple[str, ...] = ()

    @override
    def as_metadata(self) -> dict:
        return {"done": [str(step) for step in self.done]} if self.done else {}


@dataclass(frozen=True, slots=True)
class Failure(Facet):
    """Why it stopped, separated from the fact that it did."""

    key: ClassVar[str] = "failure"

    message: str = ""
    kind: str = ""


def as_metadata(facets) -> dict:
    """Merge facets into one metadata dict, keyed by facet.

    Two facets of the same kind raise rather than the last quietly winning:
    an event with two different counts is a caller bug worth seeing.
    """
    merged: dict = {}
    for facet in facets or ():
        if not facet.key:
            raise ValueError(f"{type(facet).__name__} must declare a key.")
        if facet.key in merged:
            raise ValueError(f"Two {facet.key!r} facets on one event.")
        data = facet.as_metadata()
        if data:
            merged[facet.key] = data
    return merged
