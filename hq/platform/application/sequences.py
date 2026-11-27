"""A workflow that crosses outside systems, declared once as the steps it
goes through in order (``SequenceSpec``): each step read from the one record
that holds its state, and moved by the one party its declaration names, the
person among them. A declaration turns its subject into a ``Sequence``, the
one value the card, the queue, a finding, the API and MCP read, and the
``steps`` part draws. Each step stands as asked-for work does
(``asks.Standing``), so a step done, under way, failed or waiting reads and
colors the same wherever one is drawn."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .asks import DONE, FAILED, Standing
from .entity_links import EntityLink, declared_link
from .labels import dotted
lazy from .app_update_flow import UPDATE


class By(StrEnum):
    """Who moves a step on: the person, whose press it is, or the system that takes it."""

    YOU = "you"
    HQ = "hq"
    HOST = "host"
    GITHUB = "github"
    RENOVATE = "renovate"

    @property
    def label(self) -> str:
        return _BY[self]


_BY = {By.YOU: "you", By.HQ: "HQ", By.HOST: "its host", By.GITHUB: "GitHub", By.RENOVATE: "Renovate"}


@dataclass(frozen=True, slots=True)
class Step:
    """One step: its title, how it stands (its word, its sentence and the
    page that holds it), the parts it is made of (each check, each app), who
    moves it on, and whether an event told HQ how it stands since its record
    was last read, which that record's next read replaces."""

    title: str
    standing: Standing
    parts: tuple[Step, ...] = ()
    by: By = By.HQ
    told: bool = False

    @property
    def done(self) -> bool:
        return self.standing.state == DONE

    @property
    def live(self) -> bool:
        """Whether it or a part of it is under way, so what draws it asks again until it ends."""

        return self.standing.live or any(part.live for part in self.parts)

    @property
    def detail(self) -> str:
        """Its sentence, then ``told by GitHub`` while an event says more than the last read."""

        return dotted(self.standing.note, f"told by {self.by.label}" if self.told else "")

    @property
    def link(self) -> EntityLink | None:
        """Its title as a link to the page that holds it, None where none does."""

        return declared_link(self.title, self.standing.url) if self.standing.url else None


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

        return next((step for step in self.steps if not step.done and step.by == By.YOU), None)

    @property
    def live(self) -> bool:
        return any(step.live for step in self.steps)


@dataclass(frozen=True, slots=True)
class Read:
    """What a step's reader says beyond its standing: its parts, whether an
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
    reads: Callable[[Subject], Standing | Read | None]
    by: By

    def of(self, subject: Subject) -> Step | None:
        read = self.reads(subject)
        if read is None:
            return None
        if isinstance(read, Standing):
            return Step(self.title, read, by=self.by)
        return Step(self.title, read.standing, read.parts, By.YOU if read.yours else self.by, read.told)


@dataclass(frozen=True, slots=True)
class Example:
    """A subject a seed leaves at one step: the seed by its name
    (``hq.platform.core.seeds``), the subject's key, and the step it stands
    at, by its title, in the state it stands in there; both "" once every step is done."""

    seed: str
    key: str
    at: str
    state: str


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

    def of(self, subject: Subject) -> Sequence:
        steps = (step.of(subject) for step in self.steps)
        return Sequence(self.about(subject), tuple(step for step in steps if step is not None))


def declared() -> tuple[SequenceSpec, ...]:
    """Every workflow HQ declares."""

    return (UPDATE,)
