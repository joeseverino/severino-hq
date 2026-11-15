"""The runner's consent, for a test that calls a declaration writer directly.

A writer takes a ``capabilities.Consented``, which only the runner makes, so a
test reaches one the way a person's approval does (``consented``), or through
the runner itself, as a command of its own (``another_command_writing``).
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..capabilities import Consented, execute_approved
from ..contracts import Effect
from ..integration_specs import CapabilitySpec
from ..integration_testing import commands_registered
from ..security import Capability, Principal


@dataclass(frozen=True, slots=True)
class _Nothing:
    """A command that takes nothing."""


def _handed(_command: _Nothing, *, consented: Consented) -> dict[str, Any]:
    return {"consented": consented}


_HANDS_BACK = CapabilitySpec(
    "example.consent",
    "Hand a test the consent the runner gives a run.",
    Effect.READ,
    Capability.READ,
    _Nothing,
    _handed,
)


def consented(principal: Principal) -> Consented:
    """The consent ``execute_approved`` hands a writer for ``principal``: an approved run's."""

    handed = execute_approved(_HANDS_BACK, _Nothing(), None, principal=principal, sent=frozenset())["consented"]
    if not isinstance(handed, Consented):
        raise TypeError(handed)
    return handed


# The name of the command ``another_command_writing`` registers.
ANOTHER_COMMAND = "example.things.tidy"


@contextmanager
def another_command_writing(write: Callable[[Consented], object]) -> Iterator[str]:
    """A change command about something other than declarations, whose handler
    calls ``write`` with the consent the runner gives it; yields its name.

    It never says it acts on a declaration, so the default hold does not
    judge it: whatever it writes is held to the consent alone.
    """

    def handler(_command: _Nothing, *, consented: Consented) -> dict[str, Any]:
        write(consented)
        return {"ok": True}

    spec = CapabilitySpec(
        ANOTHER_COMMAND,
        "Tidy example things.",
        Effect.REMOTE_WRITE,
        Capability.MANAGE_INFRASTRUCTURE,
        _Nothing,
        handler,
        subject_resource="example.things",
    )
    with commands_registered({spec.name: spec}):
        yield spec.name
