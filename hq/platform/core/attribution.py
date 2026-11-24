"""Who is acting and in what operation, bound where each door starts.

``audit.record_event`` refuses a row written outside a binding. Held apart from
``audit`` so a door can bind before Django's applications load.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, replace
from typing import Any

lazy from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
lazy from hq.platform.application import request_context
lazy from hq.platform.application.security import CLI_ACTOR, CLI_INTERFACE, WEB_INTERFACE

from . import bindings, tracing
lazy from .audit import made_in

# The operation a sign-in's writes are named by where no request bound one.
SIGN_IN_OPERATION = "sign_in"


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Stable attribution shared by web, MCP, and CLI adapters.

    An empty ``actor`` is a request's binding before it knows who is asking.
    ``person`` finds the account an agent acts for, asked when a row is
    written, since a binding may open on an event loop where no query runs.
    """

    interface: str
    actor: str
    operation: str
    operation_id: str = ""
    device: str = ""
    cause: str = ""
    approved_by: str = ""
    person: Callable[[], Any] | None = None


def bound() -> OperationContext | None:
    """The operation this code runs inside, or None outside every door."""

    return bindings.OPERATION.get()


@contextmanager
def _door_of_its_own(door: OperationContext) -> Iterator[None]:
    """Bind ``door`` as the outermost, whatever is bound around it, outside the
    request being answered."""

    with bindings.OPERATION.set(door), request_context.bind(None):
        yield


def own_operation(
    record: Any,
    *,
    operation: str = "",
    principal: object | None = None,
    requester: OperationContext | None = None,
) -> AbstractContextManager[None]:
    """The operation every row of ``record``'s life is part of, under its own id.

    A ``requester`` (a job's) is bound outermost, since the job's rows are its
    requester's wherever it runs. A held approval's binding may name someone
    other than whoever started the operation around it: an approved change
    runs as its requester, and a lapse as HQ. So may an operation's binding
    name its own requester, for the step it carries that runs as them.
    """

    if requester is not None:
        return _door_of_its_own(replace(requester, operation_id=str(record.pk)))
    outer = bound()
    binding = bindings.audit_operation(
        operation=operation or (outer.operation if outer is not None else ""),
        principal=principal,
        operation_id=str(record.pk),
        cause=made_in(record),
    )
    rebinds = isinstance(record, ApprovalRequest) or (
        isinstance(record, OperationRequest) and getattr(principal, "actor", None) == record.requested_actor
    )
    return _rebound(binding) if rebinds else binding


@contextmanager
def _rebound(binding: AbstractContextManager[None]) -> Iterator[None]:
    """``binding``, opened over whoever is bound; what runs inside it is not."""

    with ExitStack() as stack:
        with bindings.REBINDING.set(True):
            stack.enter_context(binding)
        yield


@contextmanager
def outside_every_door() -> Iterator[None]:
    """Nothing bound in the block, whatever is bound around it, so a row
    written there is refused unless the code that writes it binds its own door.

    For an extension's demo (``seeds``), which says how each of its events was
    made, so none takes the door of whatever runs the seed.
    """

    with bindings.OPERATION.set(None):
        yield


def bind_this_thread(*, interface: str, actor: str, operation: str) -> None:
    """Bind the calling thread for the rest of its life, under no id of its own,
    so each operation nested in it mints its own.

    For a test process, whose arrangement (a fixture, a sign-in) runs outside
    every door. A thread it starts inherits nothing, so work a door hands to a
    thread is still refused unless that door binds it.
    """

    bindings.OPERATION.set(OperationContext(interface=interface, actor=actor, operation=operation))


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
            bindings.operation_context(
                interface=self.interface, actor="", operation=self.operation, operation_id=request_id
            ),
        ):
            await self.app(scope, receive, send)


def from_the_shell(operation: str) -> AbstractContextManager[None]:
    """The shell's door: whatever is written in the block is the local operator's.

    Where ``manage.py`` binds every command, and a tool that drives Django in a
    process of its own binds its run.
    """

    return bindings.operation_context(interface=CLI_INTERFACE, actor=CLI_ACTOR, operation=operation)


def signing_in(username: str) -> AbstractContextManager[None]:
    """A sign-in's door once it knows who signed in, before the session holds
    them: what it keeps of what the provider said (a role, an account, a
    picture) is that person's doing, under the request's operation."""

    outer = bound()
    return bindings.operation_context(
        interface=WEB_INTERFACE, actor=username, operation=outer.operation if outer else SIGN_IN_OPERATION
    )


def command_operation(argv: list[str]) -> str:
    """The operation a management command runs as: ``manage.<command>``."""

    named = next((word for word in argv[1:] if not word.startswith("-")), "help")
    return f"manage.{named}"
