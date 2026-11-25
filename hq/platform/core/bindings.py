"""The two bindings that name an operation: a door's (``operation_context``)
and a principal's (``audit_operation``).

A binding inside a handler or service renames every row under it, so the log
names an act by a word no command declares. Only the doors, the capability
runner and the attribution core import this module, which lint:imports holds.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from typing import Any
from uuid import uuid7

lazy from django.contrib.auth.base_user import AbstractBaseUser

lazy from hq.platform.application.security import person_of

lazy from . import audit
lazy from .attribution import OperationContext

OPERATION: ContextVar[OperationContext | None] = ContextVar("hq_operation_context", default=None)
# True only while own_operation opens a held approval's binding.
REBINDING: ContextVar[bool] = ContextVar("hq_rebinding", default=False)


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
    approval: str = "",
    person: Callable[[], Any] | None = None,
    trace: str = "",
) -> Iterator[None]:
    """Attach adapter-neutral attribution to audit events in this operation.

    Every row inside one operation carries its one id, and the chain it is
    part of: ``trace``, else the chain of the operation it runs inside, else
    its own. Once an actor is bound,
    a nested binding naming another is refused, so no code inside can write its
    rows as someone else's, but a held approval's (``own_operation``).
    """

    outer = None if not actor else OPERATION.get()
    if outer is not None and outer.actor and not REBINDING.get() and actor != outer.actor:
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
    operation_id = operation_id or within or uuid7().hex
    if not trace and outer is not None and outer.operation_id:
        trace = outer.chain
    # A nested binding of the same caller keeps the device the outer one
    # placed, and the consent the outer one carried.
    same_caller = outer is not None and outer.actor == actor
    device = device or (outer.device if same_caller else "")
    if not approved_by and same_caller:
        approved_by, approval = outer.approved_by, outer.approval
    person = person or (outer.person if same_caller else None)
    with OPERATION.set(
        OperationContext(
            interface=interface,
            actor=actor,
            operation=operation,
            operation_id=operation_id,
            device=device,
            cause=cause,
            approved_by=approved_by,
            approval=approval if approved_by else "",
            person=person,
            trace=trace,
        )
    ):
        yield


@contextmanager
def audit_operation(
    *,
    operation: str,
    principal: object | None = None,
    operation_id: str = "",
    cause: str = "",
    trace: str = "",
) -> Iterator[None]:
    """Attribute an operation to the principal that made it: the one way a
    host service or a plugin binds a principal's interface and actor.

    Without a principal the operation is the bound caller's, and with nobody
    bound it is refused, so no operation is written down as nobody's.
    ``cause`` and ``trace`` are ``operation_context``'s.
    """

    # A plugin hands whatever principal it holds, or none.
    outer = audit.attributed(f"The operation {operation!r} began") if principal is None else OPERATION.get()
    with operation_context(
        interface=getattr(principal, "interface", None) or (outer.interface if outer else ""),
        actor=getattr(principal, "actor", None) or (outer.actor if outer else ""),
        operation=operation,
        operation_id=operation_id,
        device=getattr(principal, "device", None) or "",
        cause=cause,
        approved_by=getattr(principal, "approved_by", None) or "",
        approval=getattr(principal, "approval", None) or "",
        person=_acted_for(principal),
        trace=trace,
    ):
        yield


def _acted_for(principal: object | None) -> Callable[[], AbstractBaseUser | None] | None:
    """How to find the account a token's principal acts for, which no session
    names (``security.person_of``), looked up once, by the first row that asks."""

    if not getattr(principal, "person_key", ""):
        return None
    return cache(lambda: person_of(principal))
