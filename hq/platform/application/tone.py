"""How a thing stands, in the one vocabulary every page, finding and answer says it in.

A module of its own, imported by nothing of HQ's, so every view model can type
a field by it; ``ui`` and ``hq_sdk.ui`` export it.
"""

from collections.abc import Iterable
from enum import StrEnum


class Tone(StrEnum):
    """How a thing stands, most severe first.

    Every surface that shows state draws from it, so a state means the same
    thing and looks the same wherever it is rendered: the stylesheet keys on
    each value (``data-tone``, ``toned-<tone>``). Reserved: a tone never
    doubles as a categorical series color, and is always drawn with its own
    words, so state is never carried by color alone. A thing nothing has read
    is neutral: no fifth tone says "unknown".
    """

    SERIOUS = "serious"
    ATTENTION = "attention"
    NEUTRAL = "neutral"
    GOOD = "good"

    @property
    def rank(self) -> int:
        """Its place in the order, the most severe 0."""

        return _ORDER.index(self)

    @property
    def urgency(self) -> str:
        """How urgent, in the words a queue card's pill says."""

        return _URGENCY[self]

    @property
    def acted_on(self) -> bool:
        """Whether someone has to act: serious or attention, never context."""

        return self in (Tone.SERIOUS, Tone.ATTENTION)

    @classmethod
    def worst(cls, tones: Iterable[str], default: Tone | None = None) -> Tone:
        """The most severe of ``tones``, or ``default`` (good) when there are none."""

        return min((cls(tone) for tone in tones), key=lambda tone: tone.rank, default=default or cls.GOOD)


class Toned(StrEnum):
    """One subject's states (a machine's presence, a container's), each member
    declared beside the tone it is drawn in, ``ONLINE = "online", Tone.GOOD``.

    Its value is the words it is said in, so ``{% status machine.state %}``
    draws a state as its label in its tone and no page spells either.
    """

    tone: Tone

    def __new__(cls, value: str, tone: Tone) -> Toned:
        member = str.__new__(cls, value)
        member._value_ = value
        member.tone = tone
        return member

    @property
    def label(self) -> str:
        """The words it is said in."""

        return self.value


_ORDER = tuple(Tone)
_URGENCY = {Tone.SERIOUS: "Urgent", Tone.ATTENTION: "Needs attention", Tone.NEUTRAL: "Note", Tone.GOOD: "Fine"}
