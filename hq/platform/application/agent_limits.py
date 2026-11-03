"""How much one agent may ask of HQ: calls at once, its own and everyone's, and a call rate.

Both agent doors ask it of a caller they authenticated. It is held in this
process's memory, which every request sees because HQ serves from one process
(the image's CMD; ``test_one_serving_process`` holds it), and an agent that is
not calling holds nothing: a count back at zero and a rate fully recovered are
both removed. The settings beside ``SEVERINO_AGENT_CALLS_AT_ONCE`` set the
numbers.

The rate is a token bucket kept the way the generic cell rate algorithm keeps
one: a single time per agent, when its bucket is full again. The standard
library has no bucket; pyrate-limiter, already locked as a test tool's
dependency, keeps a timestamp per call and a background thread to expire them,
and django-ratelimit needs a cache shared across processes with an atomic
increment, and a Django request, which the MCP door does not have.
"""

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType

from django.conf import settings

from .refusal_status import Refusal

RATE_LIMITED = "rate_limited"
TOO_MANY_CALLS = "too_many_calls"


@dataclass(frozen=True, slots=True)
class Limited:
    """A call refused for how much its agent asks: a code a client keys on, a
    sentence for it, and the whole seconds after which asking again can succeed."""

    code: str
    reason: str
    retry_after: int

    @property
    def refusal(self) -> Refusal:
        """What every door answers, the wait with it."""

        return Refusal(self.code, self.reason, retry_after=self.retry_after)


class Slot:
    """One call at once, held for a ``with`` block and given back once, however the block ends."""

    def __init__(self, limits: Limits, actor: str) -> None:
        self._limits = limits
        self.actor = actor
        self._held = True

    def __enter__(self) -> Slot:
        return self

    def __exit__(self, *_: type[BaseException] | BaseException | TracebackType | None) -> None:
        self._limits.release(self)

    def given_back(self) -> bool:
        """Whether this is the first time it is given back, which marks it given back."""

        held, self._held = self._held, False
        return held


class Limits:
    """The counts and rates of the agents calling now."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._full_again: dict[str, float] = {}
        self._running: dict[str, int] = {}
        self._overall = 0

    def take(self, actor: str) -> Limited | None:
        """Spend one of ``actor``'s calls, or the refusal when it has none left."""

        spacing = 60 / settings.SEVERINO_AGENT_CALLS_PER_MINUTE
        depth = spacing * settings.SEVERINO_AGENT_CALL_BURST
        now = self._clock()
        with self._lock:
            full_again = max(self._full_again.get(actor, now), now) + spacing
            wait = full_again - now - depth
            if wait > 0:
                return Limited(RATE_LIMITED, _rate_reason(wait), math.ceil(wait))
            self._full_again[actor] = full_again
            self._forget_recovered(now)
        return None

    def occupy(self, actor: str) -> Slot | Limited:
        """One of ``actor``'s calls at once, or the refusal when it, or every agent together, has them all."""

        with self._lock:
            if self._overall >= settings.SEVERINO_AGENT_CALLS_AT_ONCE_OVERALL:
                return Limited(
                    TOO_MANY_CALLS, "HQ is answering all the agent calls it takes at once. Retry shortly.", 1
                )
            running = self._running.get(actor, 0)
            if running >= settings.SEVERINO_AGENT_CALLS_AT_ONCE:
                return Limited(
                    TOO_MANY_CALLS, f"This agent already has {running} calls in flight. Retry when one answers.", 1
                )
            self._running[actor] = running + 1
            self._overall += 1
        return Slot(self, actor)

    def release(self, slot: Slot) -> None:
        with self._lock:
            if not slot.given_back():
                return
            self._overall -= 1
            running = self._running.pop(slot.actor) - 1
            if running:
                self._running[slot.actor] = running

    def held(self) -> int:
        """How many agents this holds anything for: none once no agent is calling or recovering."""

        with self._lock:
            self._forget_recovered(self._clock())
            return len(self._full_again.keys() | self._running.keys())

    def _forget_recovered(self, now: float) -> None:
        # A bucket full again is the same as none, so it is not kept.
        for actor in [actor for actor, at in self._full_again.items() if at <= now]:
            del self._full_again[actor]


def _rate_reason(wait: float) -> str:
    return (
        f"This agent is calling faster than {settings.SEVERINO_AGENT_CALLS_PER_MINUTE} calls a minute. "
        f"Retry in {math.ceil(wait)} seconds."
    )


# The one set every request in this process shares. It is read as
# ``agent_limits.LIMITS`` where it is used, never bound by an import, so the
# fresh set the test runner gives each test is the one the doors use.
LIMITS = Limits()
