"""A workflow that crosses outside systems, declared once as the steps it
goes through in order (``SequenceSpec``): each step read from the one record
that holds its state, and moved on by the person or by a provider HQ admits,
named by its key. A declaration turns its subject into a ``Sequence``, the
one value the card, the queue, a finding, the API and MCP read, and the
``steps`` part draws. Each step stands as asked-for work does
(``asks.Standing``), so a step done, under way, failed or waiting reads and
colors the same wherever one is drawn. A domain declares its workflows as
it declares its commands, through its integration's ``sequences``."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Final, TypedDict

lazy from hq.domains.control_plane.provider_adapters import CONNECTIONS, INBOUND, OBSERVATIONS

from .asks import DONE, FAILED, IDLE, QUEUED, RUNNING, Standing, StandingAnswer
from .describe import Describe
from .entity_links import EntityLink, declared_link, provider_label
from .labels import dotted, lower_first
from .ui import ended
lazy from .domains import host_specs
lazy from .plugins import plugin_sequences

# What a step queues once the operation it asked succeeds: handed that
# operation and what it carries, the record of what it queued.
type FollowOn = Callable[[Any, dict[str, Any]], dict[str, str]]
# The mover of a step that is the person's to take; every other mover is a
# provider, by the key HQ admits it under.
PERSON: Final = "person"
# The states a step stands at while it is not done.
_OPEN = (IDLE, QUEUED, RUNNING, FAILED)
# The mark a part is drawn with, by how it stands.
_MARKS = {DONE: "check-circle", FAILED: "x-circle", RUNNING: "clock", QUEUED: "clock", IDLE: "circle"}


def _admitted(mover: str) -> bool:
    providers = {spec.provider for spec in OBSERVATIONS.values()} | CONNECTIONS.keys() | INBOUND.keys()
    return mover == PERSON or mover in providers


class PartAnswer(TypedDict):
    title: Annotated[str, Describe("What it is, in the words its page shows.")]
    standing: StandingAnswer
    by: Annotated[str, Describe("Who moves it on: `person`, or the key of the provider HQ admits.")]
    told: Annotated[bool, Describe("Whether an event told HQ how it stands since its record was last read.")]


class StepAnswer(PartAnswer):
    parts: Annotated[list[PartAnswer], Describe("What it is made of, such as each check; empty for none.")]


@dataclass(frozen=True, slots=True)
class Step:
    """One step: its title, how it stands (its word, its sentence and the
    page that holds it), the parts it is made of (each check, each app), who
    moves it on, and whether an event told HQ how it stands since its record
    was last read, which that record's next read replaces."""

    title: str
    standing: Standing
    parts: tuple[Step, ...] = ()
    by: str = PERSON
    told: bool = False

    @property
    def done(self) -> bool:
        return self.standing.state == DONE

    @property
    def live(self) -> bool:
        """Whether it or a part of it is under way, so what draws it asks again until it ends."""

        return self.standing.live or any(part.live for part in self.parts)

    @property
    def mark(self) -> str:
        return _MARKS[self.standing.state]

    @property
    def opens(self) -> bool:
        """Whether a part of it failed, so its parts are drawn open: what the person must see first."""

        return any(part.standing.state == FAILED for part in self.parts)

    @property
    def line(self) -> str:
        """Its word and its detail as one line, where it folds its parts under it."""

        return dotted(self.standing.label, self.detail)

    @property
    def detail(self) -> str:
        """Its sentence, then who told HQ of it while an event says more than the last read."""

        return dotted(self.standing.note, f"told by {provider_label(self.by)}" if self.told else "")

    @property
    def link(self) -> EntityLink | None:
        """Its title as a link to the page that holds it, None where none does."""

        return declared_link(self.title, self.standing.url) if self.standing.url else None

    @property
    def part(self) -> PartAnswer:
        return {"title": self.title, "standing": self.standing.as_json(), "by": self.by, "told": self.told}

    def as_json(self) -> StepAnswer:
        """It as the API and MCP answer it, the same the page draws."""

        return {**self.part, "parts": [part.part for part in self.parts]}


@dataclass(frozen=True, slots=True)
class Sequence:
    """One subject's way through a workflow: what it is of, and each step in order."""

    about: str
    steps: tuple[Step, ...]

    @property
    def at(self) -> Step | None:
        """The step it stands at: the first that failed, else the first not
        done; None once every step is."""

        failed = next((step for step in self.steps if step.standing.state == FAILED), None)
        return failed or next((step for step in self.steps if not step.done), None)

    @property
    def next(self) -> Step | None:
        """The one step that is the person's to take: the first not done they move."""

        return next((step for step in self.steps if not step.done and step.by == PERSON), None)

    @property
    def live(self) -> bool:
        return any(step.live for step in self.steps)

    @property
    def said(self) -> str:
        """Where it stands and the person's next step, as the line a card
        leads with says them: ``Checks running · 1 of 2 passed. Approve:
        yours, once checks pass.``; its last step once every step is done."""

        at, yours = self.at or (self.steps[-1] if self.steps else None), self.next
        if at is None:
            return ""
        lines = (
            f"{at.title} {lower_first(at.line)}" if at is not yours else "",
            f"{yours.title}: {lower_first(yours.standing.label)}" if yours else "",
        )
        return " ".join(ended(line) for line in lines if line)


@dataclass(frozen=True, slots=True)
class Read:
    """What a step's reader says of it: how it stands, its parts, whether an
    event told it, and whether the subject makes the step the person's."""

    standing: Standing
    parts: tuple[Step, ...] = ()
    told: bool = False
    yours: bool = False


@dataclass(frozen=True, slots=True)
class StepSpec[Subject]:
    """One step as its workflow declares it: its title, the reader of the one
    record it stands on, and who moves it on, which a reader may say is the
    person (``Read.yours``) where the subject makes the step theirs."""

    title: str
    # None where the subject's way does not pass through the step.
    reads: Callable[[Subject], Read | None]
    by: str
    # What it says while it waits on the steps before it, read idle with no word.
    waiting: str = "Waiting"

    def __post_init__(self) -> None:
        if not _admitted(self.by):
            raise ValueError(f"The step {self.title!r} is moved by {self.by!r}, which is no admitted provider.")

    def of(self, subject: Subject) -> Step | None:
        if (read := self.reads(subject)) is None:
            return None
        standing = read.standing
        if standing.state == IDLE and not standing.word:
            standing = replace(standing, word=self.waiting)
        return Step(self.title, standing, read.parts, PERSON if read.yours else self.by, read.told)


@dataclass(frozen=True, slots=True)
class Example:
    """A subject a seed leaves at one step: the seed by its name
    (``hq.platform.core.seeds``), the subject's key, and the step it stands at
    with the state it stands in there; no step once every step is done."""

    seed: str
    key: str
    at: StepSpec | None = None
    state: str = ""

    def __post_init__(self) -> None:
        if (self.at is None) != (self.state == "") or (self.at is not None and self.state not in _OPEN):
            raise ValueError(f"An example stands at a step in one of {_OPEN}, or at none once all are done.")


@dataclass(frozen=True, slots=True)
class SequenceSpec[Subject]:
    """A workflow declared once: its steps in order, what a subject is said
    as, how a key names one, and the subjects its examples leave at each step,
    which one test walks (``test_sequences``) and each page draws."""

    name: str
    steps: tuple[StepSpec[Subject], ...]
    about: Callable[[Subject], str]
    subject: Callable[[str], Subject]
    examples: Mapping[str, Example] = field(default_factory=dict)
    # What a step queues once the operation it asked succeeds, by that
    # operation's action: handed the operation and what it carries (its
    # ``then``), it answers what it queued (``resource_operations.succeeded``).
    follow_ons: Mapping[str, FollowOn] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if strays := [name for name, each in self.examples.items() if each.at and each.at not in self.steps]:
            raise ValueError(f"The examples {strays} of {self.name} stand at a step it does not declare.")

    def of(self, subject: Subject) -> Sequence:
        steps = (step.of(subject) for step in self.steps)
        return Sequence(self.about(subject), tuple(step for step in steps if step is not None))


def follow_on(action: str) -> FollowOn | None:
    """The step a declared workflow queues once an operation of ``action`` succeeds."""

    found = [spec.follow_ons[action] for spec in declared() if action in spec.follow_ons]
    if len(found) > 1:
        raise ValueError(f"Two workflows follow a succeeded {action!r}; one declares it.")
    return found[0] if found else None


def declared() -> tuple[SequenceSpec, ...]:
    """Every workflow the host's domains and the installed extensions declare."""

    return (*host_specs("sequences"), *plugin_sequences())
