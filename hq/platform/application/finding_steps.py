"""One step of what to do about a finding, and how an answer writes a finding's help.

Apart from ``finding_model`` so a record an answer carries can name these
without loading every detector.
"""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, with_config

from .action_links import ActionLink
from .command_fields import Describe
from .json_schemas import Written
from .workflows import WrittenAction

type StepNotes = Annotated[tuple[str, ...], Describe("What to know before or after it, a sentence each.")]


# Each has its own config, so it is written one way under every answer that holds it.
@with_config(ConfigDict(extra="forbid"))
@dataclass(frozen=True, slots=True)
class Press:
    """A command HQ runs through its own route, offered only to a reader who may run it.

    Derivation writes it, from a detector's ``FindingRemedy``: a detector
    names the command, and never the link a reader presses."""

    link: ActionLink
    kind: Annotated[Literal["press"], Describe("A command HQ runs, pressed through `link`.")] = "press"


@with_config(ConfigDict(extra="forbid"))
@dataclass(frozen=True, slots=True)
class Run:
    """A command an operator runs on their own machine. HQ never runs it."""

    label: Annotated[str, Describe("What running it does, said before the command.")]
    command: Annotated[str, Describe("The exact command to paste into a shell on the machine it names.")]
    notes: StepNotes = ()
    kind: Annotated[Literal["run"], Describe("A command the operator runs.")] = "run"

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("A Run names its command; a step said in words is a Do.")


@with_config(ConfigDict(extra="forbid"))
@dataclass(frozen=True, slots=True)
class Do:
    """What to do, in words, where no exact command can be named."""

    text: Annotated[str, Describe("What to do, in a sentence or two.")]
    notes: StepNotes = ()
    kind: Annotated[Literal["do"], Describe("An instruction in words.")] = "do"

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("A Do says what to do.")


# One step of a finding's help: each says which it is by its kind, and holds
# only what that kind has.
type Step = Annotated[
    Press | Run | Do,
    Field(discriminator="kind"),
    Describe("One step of what to do about a finding, told apart by its `kind`."),
]


def told(label: str, command: str, notes: tuple[str, ...] = ()) -> Run | Do:
    """``command`` to run where one could be written for it, else ``label`` in words."""

    return Run(label, command, notes) if command else Do(label, notes)


# A finding's steps and its check as every answer that carries them says them.
WrittenStep = Annotated[dict[str, Any], Written(Step)]
type WrittenSteps = Annotated[
    list[WrittenStep],
    Describe(
        "What to do about it, in order: a command HQ runs (`press`), a command you run on your own machine "
        "(`run`), or what to do in words (`do`)."
    ),
]
type WrittenReason = Annotated[
    str, Describe("Why HQ can neither run a fix nor name an exact command for it; empty when it can do either.")
]
type WrittenCheck = Annotated[
    WrittenAction | None,
    Describe("The press that reads again what it rests on and runs its check; null where there is none."),
]
