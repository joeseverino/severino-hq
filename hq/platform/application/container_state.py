"""How a container stands, as Docker last reported it: whether it runs, and
what its own health check says.

Two facts with one owner each, so a page that asks whether a container is up
and one that asks whether it is well never read the same word two ways.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .tone import Tone, Toned

# Docker's status ends with its health check's verdict: "Up 11 hours (healthy)".
_CHECK = re.compile(r"\s*\((healthy|unhealthy|health: [^)]*)\)\s*$")


class ContainerState(Toned):
    """A container's state, in the words of the Engine API's ``State.Status``.
    Only a running container is up; every other state is one to look at."""

    RUNNING = "running", Tone.GOOD
    CREATED = "created", Tone.SERIOUS
    PAUSED = "paused", Tone.SERIOUS
    RESTARTING = "restarting", Tone.SERIOUS
    REMOVING = "removing", Tone.SERIOUS
    EXITED = "exited", Tone.SERIOUS
    DEAD = "dead", Tone.SERIOUS
    # A record that names no state Docker has.
    UNKNOWN = "unknown", Tone.SERIOUS

    @classmethod
    def read(cls, word: str) -> ContainerState:
        """The state a record's word names, or unknown."""

        return cls(word) if word in cls else cls.UNKNOWN

    @property
    def running(self) -> bool:
        return self is ContainerState.RUNNING


@dataclass(frozen=True, slots=True)
class ContainerCount:
    """How many containers the sweep found and how many of them run: the one
    count a machine, the estate and the dashboard say."""

    running: int
    total: int

    @classmethod
    def of(cls, states: Iterable[ContainerState]) -> ContainerCount:
        found = list(states)
        return cls(running=sum(1 for state in found if state.running), total=len(found))

    @property
    def stopped(self) -> int:
        return self.total - self.running


class HealthCheck(Toned):
    """What a container's own health check last said, in Docker's word for it
    (``State.Health.Status``): passing is good, anything else is to be looked at."""

    HEALTHY = "healthy", Tone.GOOD
    UNHEALTHY = "unhealthy", Tone.ATTENTION
    STARTING = "starting", Tone.ATTENTION

    @classmethod
    def of(cls, status: str) -> HealthCheck | None:
        """The verdict Docker's status ends with, or None for a container with no check."""

        found = _CHECK.search(str(status or ""))
        word = found.group(1).removeprefix("health: ") if found else ""
        return cls(word) if word in cls else None

    @property
    def said(self) -> str:
        """The verdict as a sentence's words: ``health check passing``."""

        return _SAID[self]


_SAID = {
    HealthCheck.HEALTHY: "health check passing",
    HealthCheck.UNHEALTHY: "health check failing",
    HealthCheck.STARTING: "health check starting",
}


def uptime_of(status: str) -> str:
    """Docker's status without its health check: ``Up 11 hours``."""

    return _CHECK.sub("", str(status or "")).strip()
