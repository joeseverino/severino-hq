"""A request never waits on anything outside the process.

A page or a button answers from what HQ holds. Work that reaches a network, a
subprocess or a timer happens where waiting costs nobody: the controller reads
the outside world and reports it, and a job (``hq.domains.jobs``) does long
work on its own thread. A request stores the ask and answers at once
(``hq.platform.application.asks``).

That is held by the interpreter, not by a list of names. Python raises an audit
event (PEP 578) whenever any library opens a connection, resolves a name,
starts a process or sleeps, whichever library it is. While a request is being
served, one of those events is refused: the call raises ``OutboundInRequest``
before it leaves. So a view cannot come to wait on the network by importing
something new.

``ALLOWED`` is every exception, each with why a request must wait there. Code
enters one by name with ``allowed``; an architecture test holds the uses to
this table.
"""

from __future__ import annotations

import functools
import logging
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

from django.conf import settings

logger = logging.getLogger("severino.request")

# Where a request may wait on something outside the process, and why.
ALLOWED = MappingProxyType(
    {
        "oidc": (
            "Signing in is an exchange with the identity provider: the token, "
            "its signing keys and the person's picture are asked for while the "
            "browser waits, with the provider's own short-lived token, and "
            "each call is bounded by OIDC_TIMEOUT or the picture's timeout."
        ),
        "lookup": (
            "A lookup is one question to a public resolver or registry that an "
            "operator or a program asked for by name and waits for; it is "
            "bounded by SEVERINO_LOOKUP_TIMEOUT_SECONDS and carries no credential."
        ),
        "contacts.d1": (
            "A contact submission's email and message exist only in the "
            "site's D1 database, which HQ keeps no copy of, so its review "
            "pages read and write that store directly, bounded by D1's timeout."
        ),
    }
)

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

# The request this context is serving ("POST /watching/refresh/"), or "".
_serving: ContextVar[str] = ContextVar("hq_outbound_serving", default="")
# The entry of ``ALLOWED`` this context is inside, or "".
_allowed: ContextVar[str] = ContextVar("hq_outbound_allowed", default="")
# True while the hook is reporting, so what reporting does is not judged.
_reporting: ContextVar[bool] = ContextVar("hq_outbound_reporting", default=False)


class OutboundInRequest(RuntimeError):
    """A request tried to wait on something outside the process."""


@contextmanager
def serving(request: Any) -> Iterator[None]:
    """Mark everything inside as part of serving ``request``."""

    token = _serving.set(f"{request.method} {request.path}")
    try:
        yield
    finally:
        _serving.reset(token)


@contextmanager
def off_request() -> Iterator[None]:
    """Mark everything inside as no request's work: a job's own thread."""

    token = _serving.set("")
    try:
        yield
    finally:
        _serving.reset(token)


def serving_request() -> str:
    """The request this context is serving ("POST /example/"), or ""."""

    return _serving.get()


@contextmanager
def allowed(name: str) -> Iterator[None]:
    """Enter one of the exceptions ``ALLOWED`` names."""

    if name not in ALLOWED:
        raise ValueError(f"{name!r} is not an outbound exception HQ declares.")
    token = _allowed.set(name)
    try:
        yield
    finally:
        _allowed.reset(token)


def mode() -> str:
    """What happens to a request that reaches out: refused, or reported."""

    return REPORT if getattr(settings, "SEVERINO_OUTBOUND_IN_REQUEST", REFUSE) == REPORT else REFUSE


def _sends_nothing(event: str, args: tuple[Any, ...]) -> bool:
    """A datagram socket's connect only records where it would send: no packet
    leaves and nothing is waited for (how HQ learns its own addresses)."""

    return event == "socket.connect" and getattr(args[0], "type", None) == socket.SOCK_DGRAM


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event not in REFUSED_EVENTS:
        return
    request = _serving.get()
    if not request or _allowed.get() or _reporting.get() or _sends_nothing(event, args):
        return
    said = (
        f"{request} tried {event}. A request never waits on anything outside the "
        "process: ask the controller for a reading or start a job, and answer at once."
    )
    if mode() == REFUSE:
        raise OutboundInRequest(said)
    token = _reporting.set(True)
    try:
        logger.warning(said, extra={"event": "outbound.in_request", "audit_event": event})
    finally:
        _reporting.reset(token)


@functools.cache
def install() -> None:
    """Add the audit hook, once for the life of the process."""

    sys.addaudithook(_hook)
