"""Each finding rule shown at work, as every Part shows itself in its example.

A rule module's examples live in the module of the same name here, as
``EXAMPLES``: for each of its rules, the estates it raises something in and
what it raises there, and those it stays quiet in. One test walks them all
(``test_finding_examples``) and holds every raised finding to the
promises every finding keeps, so a new rule needs its examples and no test.
Kept with the tests, so no image ships them and loading HQ never loads them.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ...finding_steps import Do, Run
from ...tone import Tone

# The instant every example is walked at, the clock held there, so what a
# finding says of a moment is the same on every run.
AT = datetime(2026, 1, 15, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Ago:
    """An instant ``age`` before ``AT``, written as the controller writes one."""

    age: timedelta

    def written(self) -> str:
        return (AT - self.age).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**age: float) -> Ago:
    return Ago(timedelta(**age))


NOW = ago()


@dataclass(frozen=True, slots=True)
class Stored:
    """One kind's reading as a sweep stored it: its records, and the
    snapshot's own fields where they differ from a connected, reachable
    sweep's (``reachable``, ``error``, ``age``), as ``inventory_testing.store``
    takes them."""

    records: Sequence[Mapping[str, Any]] = ()
    fields: Mapping[str, Any] = field(default_factory=dict)


def stored(*records: Mapping[str, Any], **fields: Any) -> Stored:
    return Stored(records, fields)


@dataclass(frozen=True, slots=True, kw_only=True)
class Raised:
    """What one finding says that tells it apart: its title, its severity
    (its rule's, unless given) and the commands HQ offers to run, and, where
    an example is about them, its evidence, what a person does about it and
    the label and address of what its card leads with."""

    title: str
    severity: Tone | None = None
    presses: tuple[str, ...] = ()
    evidence: tuple[tuple[str, str], ...] | None = None
    steps: tuple[Run | Do, ...] | None = None
    primary: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Example:
    """An estate a rule reads: the readings stored for it, by kind;
    ``given``, which writes the rows it holds beyond them (a declared
    resource, a record of HQ's own) and may return what the estate is read
    inside (a connection family offered); the settings HQ runs under and the
    addresses its own machine answers on beside loopback; and what the rule
    raises there, in title order, a title alone where that tells it apart."""

    readings: Mapping[str, Sequence[Mapping[str, Any]] | Stored] = field(default_factory=dict)
    given: Callable[[], object] | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    hq_at: tuple[str, ...] = ()
    raises: tuple[Raised | str, ...] = ()

    def __post_init__(self) -> None:
        said = tuple(Raised(title=item) if isinstance(item, str) else item for item in self.raises)
        object.__setattr__(self, "raises", said)


@dataclass(frozen=True, slots=True)
class Examples:
    """A rule's examples: at least one estate it raises something in, and one it is quiet in."""

    rule: str
    fires: tuple[Example, ...]
    quiet: tuple[Example, ...]

    def __post_init__(self) -> None:
        if not self.fires or not all(example.raises for example in self.fires):
            raise ValueError(f"Rule {self.rule!r} needs an example it raises something in, saying what.")
        if not self.quiet or any(example.raises for example in self.quiet):
            raise ValueError(f"Rule {self.rule!r} needs an example it is quiet in.")
