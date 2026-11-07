"""Who is acting and in what operation, bound where each door starts.

Every audit row is written inside one of these bindings (``audit.record_event``
refuses a row written outside them), so the binding is made where a caller
arrives: the web's request middleware, the MCP and bridge mounts, the job
runner, and ``manage.py`` for every command. Held apart from ``audit`` so a door
can bind before Django's applications are loaded.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid7

lazy from hq.platform.application.security import CLI_ACTOR, CLI_INTERFACE

from .logging import set_request_id


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Stable attribution shared by web, MCP, and CLI adapters.

    An empty ``actor`` is a request's binding before it knows who is asking:
    each row names whoever the request has signed in when it is written.
    ``device`` is the tailnet device an agent's call came from
    (``Principal.device``), empty where HQ could not place it.
    """

    interface: str
    actor: str
    operation: str
    operation_id: str = ""
    device: str = ""


_operation_context: ContextVar[OperationContext | None] = ContextVar("hq_operation_context", default=None)


def bound() -> OperationContext | None:
    """The operation this code runs inside, or None outside every door."""

    return _operation_context.get()


class Rebound(RuntimeError):
    """A binding opened inside an operation naming someone other than whoever started it."""


@contextmanager
def operation_context(
    *, interface: str, actor: str, operation: str, operation_id: str = "", device: str = "", rebinds: bool = False
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
    operation_id = operation_id or (outer.operation_id if outer is not None else "") or str(uuid7())
    # A nested binding of the same caller keeps the device the outer one placed.
    device = device or (outer.device if outer is not None and outer.actor == actor else "")
    with _operation_context.set(
        OperationContext(
            interface=interface,
            actor=actor,
            operation=operation,
            operation_id=operation_id,
            device=device,
        )
    ):
        yield


@contextmanager
def door_of_its_own(*, interface: str, actor: str, operation: str, operation_id: str) -> Iterator[None]:
    """Bind an operation as the outermost, whatever is bound around it.

    For the job runner, whose job's rows are its requester's wherever the job runs,
    on its own thread, to its end on a caller's, or ended by the sweep that
    finds it lost; and for the example estate's doors, each bound as its real one
    binds it whatever runs the seed.
    """

    with (
        _operation_context.set(None),
        operation_context(interface=interface, actor=actor, operation=operation, operation_id=operation_id),
    ):
        yield


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
        request_id = uuid7().hex
        with (
            set_request_id(request_id),
            operation_context(interface=self.interface, actor="", operation=self.operation, operation_id=request_id),
        ):
            await self.app(scope, receive, send)


def from_the_shell(operation: str) -> AbstractContextManager[None]:
    """The shell's door: whatever is written in the block is the local operator's.

    Where ``manage.py`` binds every command, and a tool that drives Django in a
    process of its own binds its run.
    """

    return operation_context(interface=CLI_INTERFACE, actor=CLI_ACTOR, operation=operation)


def command_operation(argv: list[str]) -> str:
    """The operation a management command runs as: ``manage.<command>``."""

    named = next((word for word in argv[1:] if not word.startswith("-")), "help")
    return f"manage.{named}"
