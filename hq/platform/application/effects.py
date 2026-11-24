"""What running a command does, apart from the registries that declare one,
so a page control or a link can be typed by it without loading validation."""

from enum import StrEnum
from typing import Self

from hq.platform.application.tone import Tone


# A spec's field typed by it takes the value's string and holds the member, so
# an effect nothing declares fails where it is written.
class Effect(StrEnum):
    """What running a command does, least consequential first."""

    # The words a page says it in, the words every reference of the machine
    # contract reads beside the name (the API document, MCP's capability
    # catalog), and the tone a page says it in beside a command.
    label: str
    meaning: str
    tone: Tone

    READ = "read", "Reads only", "Reads and answers; changes nothing.", Tone.NEUTRAL
    # Its act is filed as a read request, which the audit log leaves out until asked.
    READ_REQUEST = (
        "read_request",
        "Asks for a reading",
        "Asks for a fresh reading, which the controller takes with its own credentials; changes nothing it reads.",
        Tone.NEUTRAL,
    )
    REMOTE_WRITE = (
        "remote_write",
        "Changes HQ",
        "Changes a record, in HQ or through a connection HQ holds.",
        Tone.NEUTRAL,
    )
    INFRASTRUCTURE_CHANGE = (
        "infrastructure_change",
        "Changes a live system",
        "Changes a live system HQ manages, such as a DNS record, a proxy route or a container.",
        Tone.ATTENTION,
    )
    DESTRUCTIVE = (
        "destructive",
        "Stops or deletes",
        "Stops or deletes something; running it again does not bring it back.",
        Tone.SERIOUS,
    )

    # Defaulted so ``Effect("read")``, a lookup by value, reads as one to a type checker.
    def __new__(cls, value: str, label: str = "", meaning: str = "", tone: Tone = Tone.NEUTRAL) -> Self:
        member = str.__new__(cls, value)
        member._value_ = value
        member.label = label
        member.meaning = meaning
        member.tone = tone
        return member

    @property
    def rank(self) -> int:
        """Its place in the order, a read 0: a higher rank does more."""

        return list(Effect).index(self)

    @property
    def changes(self) -> bool:
        """Whether running it records anything, an ask for a reading included."""

        return self is not Effect.READ
