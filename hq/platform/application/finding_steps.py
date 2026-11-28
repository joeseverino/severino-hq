"""What can be done about a finding: a command HQ runs, one a person runs by
hand and why, or what to do in words; and how an answer writes them.

Apart from ``finding_model`` so a record an answer carries can name these
without loading every detector.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field

from .action_links import ActionLink
from .command_fields import Describe
from .display_words import QUOTED, display
from .effects import Effect
from .json_schemas import Written
lazy from .capabilities import capability_spec, capability_words
lazy from .pages import command_reach

type StepNotes = Annotated[tuple[str, ...], Describe("What to know before or after it, a sentence each.")]


class Why(StrEnum):
    """Why a command is a person's to run rather than HQ's: the closed set of
    reasons, each said as the sentence a reader is shown."""

    SUDO = "It asks for a sudo password HQ does not hold."
    TOUCH_ID = "It asks for Touch ID, which only you can give."
    LOGIN = "It asks for a sign-in only you can make."
    OTHER_MACHINE = "It runs on a machine HQ has no way to reach."
    FIRST_INSTALL = "HQ is not set up there yet, so nothing of HQ's can run it."
    CHANNEL = "It restores the way HQ reaches the machine, so HQ cannot do it through that way."
    NOT_GRANTED = "HQ is not granted this."


@dataclass(frozen=True, slots=True)
class Press:
    """A command HQ runs through its own route, offered only to a reader who may run it.

    Derivation writes it, from a detector's ``Does``: a detector names the
    command, and never the link a reader presses."""

    link: ActionLink
    kind: Annotated[Literal["press"], Describe("A command HQ runs, pressed through `link`.")] = "press"


# A machine name a shell takes as one word.
_MACHINE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@display
class ByHand:
    """A command a person runs on their own machine, and why HQ does not."""

    label: Annotated[str, Describe("What running it does, said before the command.")]
    command: Annotated[str, Describe("The exact command to paste into a shell on the machine it names.")] = field(
        metadata=QUOTED
    )
    why: Why
    machine: Annotated[str, Describe("The machine it acts on over ssh; empty for one run where you are.")] = ""
    notes: StepNotes = ()
    kind: Annotated[Literal["run"], Describe("A command you run.")] = "run"

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("A ByHand names its command; a step said in words is a Do.")

    @classmethod
    def on(cls, machine: str, label: str, command: str, why: Why, notes: tuple[str, ...] = ()) -> tuple[ByHand, ...]:
        """``command`` as root on ``machine``, pasted over ssh, or nothing where
        the name is not one a shell takes as one word, so no step carries a
        command built on a guess. The command never says sudo itself: this adds it."""

        if command.startswith("sudo "):
            raise ValueError("A command on a machine is run as root by ByHand.on; it does not say sudo.")
        if not _MACHINE_NAME.match(machine or "") or not command.strip():
            return ()
        escaped = re.sub(r'(["\\$`])', r"\\\1", command.strip())
        return (cls(label, f'ssh {machine} "sudo {escaped}"', why, machine, notes),)


@dataclass(frozen=True, slots=True)
class Does:
    """A registered command HQ runs about a finding, offered as a press to a
    reader who may run it and as ``by_hand`` to one who may not.

    It names no command line, so nothing HQ can do is handed out to paste.
    ``intent`` is its words for the press where the command's would not say
    it; ``url`` and ``method`` name a route that runs it on something other
    than its target."""

    capability: str
    target: str = ""
    by_hand: ByHand | None = None
    intent: str = ""
    variant: str = ""
    url: str = ""
    method: str = "GET"

    def __post_init__(self) -> None:
        # The one place a fix names a command, so the one place it is held to
        # being one HQ has and one a press may run.
        spec = capability_spec(self.capability)
        if spec is None:
            raise ValueError(f"{self.capability!r} is not a command HQ has.")
        if spec.effect == Effect.DESTRUCTIVE:
            raise ValueError(f"{self.capability!r} is destructive, and a finding never offers it as a press.")

    @property
    def label(self) -> str:
        return self.intent or capability_words(self.capability, self.variant)

    @property
    def effect(self) -> str:
        """What running it does, as its command declares."""

        spec = capability_spec(self.capability)
        return spec.effect if spec else ""

    @property
    def reach(self) -> tuple[str, str]:
        """Where it is pressed, and how: its own route, else where its command is reached."""

        if self.url:
            return self.url, self.method
        url, answers_get = command_reach(self.capability, self.target, self.variant)
        return url, "GET" if answers_get else "POST"


@display
class Do:
    """What to do, in words, where no exact command can be named, with the
    value it has the reader set or copy, drawn as code since words cannot hold it."""

    text: Annotated[str, Describe("What to do, in a sentence or two.")]
    notes: StepNotes = ()
    value: Annotated[str, Describe("What it has the reader set or paste, exactly; empty when it has none.")] = field(
        default="", metadata=QUOTED
    )
    kind: Annotated[Literal["do"], Describe("An instruction in words.")] = "do"

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("A Do says what to do.")


# What can be done about a finding: HQ does it, or a person does it by hand,
# and the two never overlap, since a ``Does`` holds no command line.
type Fix = Does | ByHand

# What a detector says to do about its finding, in order.
type Advice = Fix | Do

# One step of a finding's help as a reader is shown it: each says which it is
# by its kind, and holds only what that kind has.
type Step = Annotated[
    Press | ByHand | Do,
    Field(discriminator="kind"),
    Describe("One step of what to do about a finding, told apart by its `kind`."),
]


def told(label: str, command: str, why: Why, notes: tuple[str, ...] = ()) -> ByHand | Do:
    """``command`` to run by hand where one could be written for it, else ``label`` in words."""

    return ByHand(label, command, why, notes=notes) if command else Do(label, notes)


# A finding's steps and its check as every answer that carries them says them.
WrittenStep = Annotated[dict[str, Any], Written(Step)]
type WrittenSteps = Annotated[
    list[WrittenStep],
    Describe(
        "What to do about it, in order: a command HQ runs (`press`), a command you run on your own machine "
        "with why it is yours (`run`), or what to do in words (`do`)."
    ),
]
type WrittenReason = Annotated[
    str, Describe("Why HQ can neither run a fix nor name an exact command for it; empty when it can do either.")
]
