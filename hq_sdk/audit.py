"""Adapter-neutral audit attribution for plugin operations."""

from collections.abc import Iterator
from contextlib import contextmanager

from hq.platform.core import audit
from hq.platform.core.audit import (
    audit_events,
    record_event,
    record_operation,
    register_audit,
)

# Typed metadata, exported so an extension records "12 updated, 1 failed" in
# the vocabulary every other event already uses, so `metadata` stays comparable
# across callers.
from hq.platform.core.facets import Counts, Failure, Source, Steps, Timing
from hq.platform.core.models import AuditLog

# An extension's demo writes each audited record inside one, so its event reads
# as the person it stands for made it, when they did (``PluginIntegration.demo_seed``).
from hq.platform.core.seeds import seeded_by_hand

AuditAction = AuditLog.Action


# The two bindings are the host's without its doors' powers: writing a change
# as someone other than whoever started the operation (``rebinds``) is the
# host's approvals' alone, and opening a door that starts afresh (an empty
# actor) the host's doors', so a plugin's nested binding keeps the actor in force.
@contextmanager
def audit_operation(*, operation: str, principal: object | None = None, operation_id: str = "") -> Iterator[None]:
    """Attribute an operation to the principal that made it, or to the bound caller without one."""

    with audit.audit_operation(operation=operation, principal=principal, operation_id=operation_id):
        yield


@contextmanager
def operation_context(*, interface: str, actor: str, operation: str, operation_id: str = "") -> Iterator[None]:
    """Bind who acts, through which door, for the rows written in the block."""

    # An empty actor is a door awaiting sign-in, which only the host's doors open.
    if not actor:
        raise ValueError(f"{operation!r} names nobody: a plugin's binding names who acts.")
    with audit.operation_context(interface=interface, actor=actor, operation=operation, operation_id=operation_id):
        yield


__all__ = [
    "AuditAction",
    "Counts",
    "Failure",
    "Source",
    "Steps",
    "Timing",
    "audit_events",
    "audit_operation",
    "operation_context",
    "record_event",
    "record_operation",
    "register_audit",
    "seeded_by_hand",
]
