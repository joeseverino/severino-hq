"""The listen streams an agent holds open (``subscriptions/listen``), and what reaches them.

HQ publishes one event, the tools ``list_changed``: Claude Code's MCP docs
name list changes as what it hears on a stream, and HQ's prompts and
resources change only with a release. The SDK's ``ListenHandler`` serves each
stream; this module is the bus it is given, the SDK's in-memory one with the
brakes in front of it, since the SDK delivers to every stream alike and an
event carries nobody's name:

- A stream records its caller when it opens, in the request's own task, where
  the caller is bound.
- An event is delivered only to the streams it is aimed at, and of those only
  to a caller ``agent_access.refusal`` lets act now, asked on a pooled thread
  with connections of its own. A paused, blocked or unapproved caller keeps
  its stream, as a paused agent keeps its connection, and is owed the event
  until a person lifts the brake (``agent_registry.STANDING_CHANGED``).
- Changes are told from synchronous code through ``agent_registry``'s
  signals, received here and handed to the loop each stream is served on.

An open stream holds no thread and no database connection (``MCPBoundary``
lets the request's own go after its admission), and ends when its token
expires (``ends_at``). The process serves from one loop
(``test_one_serving_process``), so this bus reaches every stream.
"""

import asyncio
import threading
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from asgiref.sync import sync_to_async
from django.dispatch import receiver
from mcp.server.subscriptions import InMemorySubscriptionBus, ServerEvent, ToolsListChanged

from hq.platform.application import agent_access
from hq.platform.application.agent_registry import STANDING_CHANGED, TOOLS_CHANGED, Aimed
from hq.platform.application.security import Principal
from hq.platform.core.outside_requests import connections_of_its_own

from .identity import caller_address, current_principal

# When the listen stream this request opens must end: its token's expiry, as
# MCPBoundary read it from the token it admitted.
_ends_at: ContextVar[float | None] = ContextVar("hq_mcp_listen_ends_at", default=None)


@contextmanager
def ending_at(moment: float | None) -> Iterator[None]:
    """A listen stream opened in the block ends at ``moment``, on the loop's clock, or with its client for None."""

    with _ends_at.set(moment):
        yield


def ends_at() -> float | None:
    """When this request's listen stream ends, on the loop's clock; None outside the door."""

    return _ends_at.get()


def everyone(_principal: Principal) -> bool:
    return True


@dataclass(eq=False, slots=True)
class Stream:
    """One open stream: who opened it, from where, the loop serving it, and what it is owed."""

    principal: Principal
    source: str
    loop: asyncio.AbstractEventLoop
    owed: set[ServerEvent] = field(default_factory=set)


class Streams:
    """The SDK's in-memory bus, delivering each event only to the streams whose caller may act now."""

    def __init__(self) -> None:
        self._bus = InMemorySubscriptionBus()
        self._lock = threading.Lock()
        self._open: list[Stream] = []
        # The streams the event being published reaches. Set just before the
        # SDK's bus calls its listeners, which it does before its first await,
        # so each publish reads its own.
        self._reaching: frozenset[Stream] = frozenset()

    def subscribe(self, listener: Callable[[ServerEvent], None]) -> Callable[[], None]:
        """Register one stream's ``listener`` for the caller of this request."""

        stream = Stream(current_principal(), caller_address(), asyncio.get_running_loop())

        def deliver(event: ServerEvent) -> None:
            if stream in self._reaching:
                listener(event)

        unsubscribe = self._bus.subscribe(deliver)
        with self._lock:
            self._open.append(stream)

        def close() -> None:
            with self._lock:
                if stream in self._open:
                    self._open.remove(stream)
            unsubscribe()

        return close

    def open_streams(self) -> list[Stream]:
        with self._lock:
            return list(self._open)

    async def publish(self, event: ServerEvent, *, aimed: Aimed = everyone) -> None:
        """Deliver ``event`` to each stream on this loop whose caller ``aimed`` admits and the brakes let act."""

        loop = asyncio.get_running_loop()
        await self._deliver(event, [s for s in self.open_streams() if s.loop is loop and aimed(s.principal)])

    async def settle_owed(self) -> None:
        """Deliver each stream on this loop what it was held back, to each caller now let act."""

        loop = asyncio.get_running_loop()
        owing = [stream for stream in self.open_streams() if stream.loop is loop and stream.owed]
        for event in {event for stream in owing for event in stream.owed}:
            await self._deliver(event, [stream for stream in owing if event in stream.owed])

    async def _deliver(self, event: ServerEvent, streams: list[Stream]) -> None:
        if not streams:
            return
        refused = await sync_to_async(_refused, thread_sensitive=False)(streams)
        for stream in streams:
            if stream in refused:
                stream.owed.add(event)
            else:
                stream.owed.discard(event)
        self._reaching = frozenset(streams) - refused
        await self._bus.publish(event)

    def soon(self, publish: Callable[[Streams], Coroutine[Any, Any, None]]) -> None:
        """Run ``publish(self)`` on each loop serving a stream, from any thread."""

        for loop in {stream.loop for stream in self.open_streams()}:
            asyncio.run_coroutine_threadsafe(publish(self), loop)


def _refused(streams: list[Stream]) -> frozenset[Stream]:
    """The streams whose caller the brakes refuse now: the question a tool call is asked, unrecorded."""

    with connections_of_its_own():
        return frozenset(
            stream for stream in streams if agent_access.refusal(stream.principal, source=stream.source) is not None
        )


# The one bus every server in this process publishes to, as every stream it
# serves is in this process's memory.
STREAMS = Streams()


@receiver(TOOLS_CHANGED)
def _tools_changed(*, aimed: Aimed, **_: object) -> None:
    STREAMS.soon(lambda streams: streams.publish(ToolsListChanged(), aimed=aimed))


@receiver(STANDING_CHANGED)
def _standing_changed(**_: object) -> None:
    STREAMS.soon(lambda streams: streams.settle_owed())
