"""Who is acting and in what operation, bound where each door starts.

Every audit row is written inside one of these bindings (``audit.record_event``
refuses a row written outside them), so the binding is made where a caller
arrives: the web's request middleware, the MCP and bridge mounts, the job
runner, ``manage.py`` for every command, and a sign-in for what it keeps.
Held apart from ``audit`` so a door can bind before Django's applications are
loaded.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid7

lazy from hq.platform.application import request_context
lazy from hq.platform.application.security import CLI_ACTOR, CLI_INTERFACE, WEB_INTERFACE

from . import tracing
lazy from .audit import audit_operation, made_in

# The operation a sign-in's writes are named by where no request bound one.
SIGN_IN_OPERATION = "sign_in"


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Stable attribution shared by web, MCP, and CLI adapters.

    An empty ``actor`` is a request's binding before it knows who is asking:
    each row names whoever the request has signed in when it is written.
    ``device`` is the tailnet device an agent's call came from
    (``Principal.device``), empty where HQ could not place it. ``cause`` is
    the operation that started this one, empty for one that started itself.
    ``approved_by`` is who agreed to the act where one had to
    (``Principal.approved_by``), so each row of it says who allowed it.
    ``person`` finds the account an agent's call acts for, whom each row names
    as its user beside the agent as its actor. It is asked when a row is
    written, since a binding may open on an event loop, where no query runs.
    """

    interface: str
    actor: str
    operation: str
    operation_id: str = ""
    device: str = ""
    cause: str = ""
    approved_by: str = ""
    person: Callable[[], Any] | None = None


_operation_context: ContextVar[OperationContext | None] = ContextVar("hq_operation_context", default=None)


def bound() -> OperationContext | None:
    """The operation this code runs inside, or None outside every door."""

    return _operation_context.get()


class Rebound(RuntimeError):
    """A binding opened inside an operation naming someone other than whoever started it."""


@contextmanager
def operation_context(
    *,
    interface: str,
    actor: str,
    operation: str,
    operation_id: str = "",
    device: str = "",
    cause: str = "",
    approved_by: str = "",
    person: Callable[[], Any] | None = None,
    rebinds: bool = False,
) -> Iterator[None]:
    """Attach adapter-neutral attribution to audit events in this operation.

    Every row written inside one operation carries its one id, so the log reads
    as events: an id given here wins, a binding nested in another keeps the
    outer one's, and the outermost binding without one mints it.

    Who acted is whoever started the operation: once an actor is bound, a
    nested binding naming another is refused, so no code inside can write its
    rows as someone else's. The same actor may name another door (a job's
    work through its requester's principal). A door bound before it knows who
    is asking (no actor yet) starts an operation of its own, as a served
    request does, even one a tool serves in its own process, and is named by
    the first binding inside it. ``rebinds`` is for the host's held changes
    alone, which the approvals service writes as someone other than whoever is
    bound: the requester an approved change runs as, and the sweep that lapses
    an expired one.
    """

    outer = None if not actor else _operation_context.get()
    if outer is not None and outer.actor and not rebinds and actor != outer.actor:
        raise Rebound(
            f"{operation!r} binds {interface}/{actor or '(nobody)'} inside {outer.operation!r}, "
            f"which {outer.interface}/{outer.actor} started. An operation's rows are its starter's: "
            "run the inner work under the binding in force."
        )
    within = outer.operation_id if outer is not None else ""
    if operation_id and operation_id != within:
        cause = cause or within
    elif not cause and outer is not None:
        cause = outer.cause
    operation_id = operation_id or within or str(uuid7())
    # A nested binding of the same caller keeps the device the outer one
    # placed, and the consent the outer one carried.
    same_caller = outer is not None and outer.actor == actor
    device = device or (outer.device if same_caller else "")
    approved_by = approved_by or (outer.approved_by if same_caller else "")
    person = person or (outer.person if same_caller else None)
    with _operation_context.set(
        OperationContext(
            interface=interface,
            actor=actor,
            operation=operation,
            operation_id=operation_id,
            device=device,
            cause=cause,
            approved_by=approved_by,
            person=person,
        )
    ):
        yield


@contextmanager
def door_of_its_own(door: OperationContext) -> Iterator[None]:
    """Bind ``door`` as the outermost, whatever is bound around it, outside the
    request being answered: for a record whose rows are its requester's
    wherever it runs (``own_operation``), and the example estate's doors."""

    with _operation_context.set(door), request_context.bind(None):
        yield


def own_operation(
    record: Any,
    *,
    operation: str = "",
    principal: object | None = None,
    rebinds: bool = False,
    requester: OperationContext | None = None,
) -> AbstractContextManager[None]:
    """The operation every row of ``record``'s life is part of, under its own
    id: a job's work and end, a held change's request, decision, application
    and lapse, a queued operation's claim and report.

    By ``principal``, else whoever is bound, under the bound operation's name
    where ``operation`` names none, and caused by the operation that made
    ``record`` (``audit.made_in``). A ``requester`` (a job's) is bound as the
    outermost, since the job's rows are its requester's wherever it runs.
    """

    if requester is not None:
        return door_of_its_own(replace(requester, operation_id=str(record.pk)))
    outer = bound()
    return audit_operation(
        operation=operation or (outer.operation if outer is not None else ""),
        principal=principal,
        operation_id=str(record.pk),
        cause=made_in(record),
        rebinds=rebinds,
    )


@contextmanager
def outside_every_door() -> Iterator[None]:
    """Nothing bound in the block, whatever is bound around it, so a row
    written there is refused unless the code that writes it binds its own door.

    For an extension's demo (``seeds``), which says how each of its events was
    made, so none takes the door of whatever runs the seed.
    """

    with _operation_context.set(None):
        yield


def bind_this_thread(*, interface: str, actor: str, operation: str) -> None:
    """Bind the calling thread for the rest of its life, under no id of its own,
    so each operation nested in it mints its own.

    For a test process, whose arrangement (a fixture, a sign-in) runs outside
    every door. A thread it starts inherits nothing, so work a door hands to a
    thread is still refused unless that door binds it.
    """

    _operation_context.set(OperationContext(interface=interface, actor=actor, operation=operation))


class BoundDoor:
    """An ASGI door beside Django's, binding each request where it arrives as
    the request middleware binds Django's: one operation, under the id its log
    lines carry, by whoever it signed in."""

    def __init__(self, app: Any, *, interface: str, operation: str) -> None:
        self.app = app
        self.interface = interface
        self.operation = operation

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with (
            tracing.trace(
                f"{scope['method']} {scope['path']}", **{"http.request.method": scope["method"]}
            ) as request_id,
            operation_context(interface=self.interface, actor="", operation=self.operation, operation_id=request_id),
        ):
            await self.app(scope, receive, send)


def from_the_shell(operation: str) -> AbstractContextManager[None]:
    """The shell's door: whatever is written in the block is the local operator's.

    Where ``manage.py`` binds every command, and a tool that drives Django in a
    process of its own binds its run.
    """

    return operation_context(interface=CLI_INTERFACE, actor=CLI_ACTOR, operation=operation)


def signing_in(username: str) -> AbstractContextManager[None]:
    """A sign-in's door once it knows who signed in, before the session holds
    them: what it keeps of what the provider said (a role, an account, a
    picture) is that person's doing, under the request's operation."""

    outer = bound()
    return operation_context(
        interface=WEB_INTERFACE, actor=username, operation=outer.operation if outer else SIGN_IN_OPERATION
    )


def command_operation(argv: list[str]) -> str:
    """The operation a management command runs as: ``manage.<command>``."""

    named = next((word for word in argv[1:] if not word.startswith("-")), "help")
    return f"manage.{named}"
