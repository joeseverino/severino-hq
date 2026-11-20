"""A request never waits on anything outside the process.

Held by the interpreter rather than a list of names: while a request is served,
the audit event (PEP 578) of any connection, name lookup, process or sleep
raises ``OutboundInRequest`` before the call leaves, so a view cannot come to
wait by importing something new. ``outbound_allowed`` holds every exception,
with why, and lets only the places that wait import it.
"""

import functools
import logging
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from django.conf import settings

from . import outbound_allowed

logger = logging.getLogger("severino.request")

# What a request may not do: reach a network, resolve a name, start a process,
# or sleep. Named by the interpreter's own audit events, so every library that
# does one of these raises it.
REFUSED_EVENTS = frozenset(
    {
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "subprocess.Popen",
        "os.system",
        "os.posix_spawn",
        "os.exec",
        "time.sleep",
    }
)

REFUSE = "refuse"
REPORT = "report"

# True while the hook is reporting, so what reporting does is not judged.
_reporting: ContextVar[bool] = ContextVar("hq_outbound_reporting", default=False)


class OutboundInRequest(RuntimeError):
    """A request tried to wait on something outside the process."""


@contextmanager
def serving(request: Any) -> Iterator[None]:
    """Mark everything inside as part of serving ``request``."""

    with outbound_allowed.serving.set(f"{request.method} {request.path}"):
        yield


@contextmanager
def off_request() -> Iterator[None]:
    """Mark everything inside as no request's work: a job's own thread."""

    with outbound_allowed.serving.set(""):
        yield


def serving_request() -> str:
    """The request this context is serving ("POST /example/"), or ""."""

    return outbound_allowed.serving.get()


def mode() -> str:
    """What happens to a request that reaches out: refused, or reported."""

    return REPORT if settings.SEVERINO_OUTBOUND_IN_REQUEST == REPORT else REFUSE


def _sends_nothing(event: str, args: tuple[Any, ...]) -> bool:
    """A datagram socket's connect only records where it would send: no packet
    leaves and nothing is waited for (how HQ learns its own addresses)."""

    # socket.connect is raised with the socket itself first.
    return event == "socket.connect" and args[0].type == socket.SOCK_DGRAM


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event not in REFUSED_EVENTS:
        return
    request = outbound_allowed.serving.get()
    if not request or outbound_allowed.entered.get() or _reporting.get() or _sends_nothing(event, args):
        return
    said = (
        f"{request} tried {event}. A request never waits on anything outside the "
        "process: ask the controller for a reading or start a job, and answer at once."
    )
    if mode() == REFUSE:
        raise OutboundInRequest(said)
    with _reporting.set(True):
        logger.warning(said, extra={"event": "outbound.in_request", "audit_event": event})


@functools.cache
def install() -> None:
    """Add the audit hook, once for the life of the process."""

    sys.addaudithook(_hook)
