"""Work HQ runs outside Django's request cycle, given what the cycle gives a request.

A background thread, a mounted ASGI application or a loop of its own gets none
of Django's connection checks, so each enters one of these. A connection inside
an open transaction is left to whoever opened it.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.db import close_old_connections, connections


def _outside_transactions() -> bool:
    return not any(connection.in_atomic_block for connection in connections.all(initialized_only=True))


def _checked() -> None:
    """A broken or expired connection is closed, as when a request starts."""

    if _outside_transactions():
        close_old_connections()


def _released() -> None:
    """Every connection this thread opened is closed, as when a request ends."""

    if _outside_transactions():
        connections.close_all()


@contextmanager
def connections_of_its_own() -> Iterator[None]:
    """Around synchronous work on a thread Django did not start: checked before, closed after."""

    _checked()
    try:
        yield
    finally:
        _released()


@asynccontextmanager
async def thread_of_its_own() -> AsyncIterator[None]:
    """Around asynchronous work whose synchronous calls get a thread of their own, as a view's do.

    Every ``sync_to_async`` call inside runs on that one thread, whose
    connections are checked before the block and closed after it, so calls in
    flight together never queue on asgiref's one process-wide thread or share
    a connection.
    """

    async with ThreadSensitiveContext():
        await sync_to_async(_checked)()
        try:
            yield
        finally:
            await sync_to_async(_released)()
