"""
Audit helpers.

Every domain model is registered via ``register_audit(Model, type_label)``.
post_save / post_delete signals then write to AuditLog, attributing the change
to the current request user (via CurrentUserMiddleware).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .middleware import get_current_user
from .models import AuditLog


logger = logging.getLogger("severino.audit")

_AUDITED_MODELS: dict[type, str] = {}
_operation_context: ContextVar["OperationContext | None"] = ContextVar(
    "hq_operation_context", default=None
)


@dataclass(frozen=True)
class OperationContext:
    """Stable attribution shared by web, MCP, and CLI adapters."""

    interface: str
    actor: str
    operation: str
    operation_id: str = ""


@contextmanager
def operation_context(
    *, interface: str, actor: str, operation: str, operation_id: str = ""
):
    """Attach adapter-neutral attribution to audit events in this operation."""

    token = _operation_context.set(
        OperationContext(
            interface=interface,
            actor=actor,
            operation=operation,
            operation_id=operation_id,
        )
    )
    try:
        yield
    finally:
        _operation_context.reset(token)


@contextmanager
def audit_operation(*, operation: str, principal=None, operation_id: str = ""):
    """Attribute a plugin operation without restating adapter fallbacks."""

    with operation_context(
        interface=getattr(principal, "interface", None) or "cli",
        actor=getattr(principal, "actor", None) or "local-operator",
        operation=operation,
        operation_id=operation_id,
    ):
        yield


def register_audit(model, type_label: str) -> None:
    """Register a model so create/update/delete events land in the audit log."""

    if model in _AUDITED_MODELS:
        return
    _AUDITED_MODELS[model] = type_label

    @receiver(post_save, sender=model, weak=False)
    def _on_save(sender, instance, created, **kwargs):
        record_event(
            action=AuditLog.Action.CREATED if created else AuditLog.Action.UPDATED,
            obj=instance,
            type_label=type_label,
        )

    @receiver(post_delete, sender=model, weak=False)
    def _on_delete(sender, instance, **kwargs):
        record_event(
            action=AuditLog.Action.DELETED,
            obj=instance,
            type_label=type_label,
        )


def audited_models() -> Iterable[tuple[type, str]]:
    return tuple(_AUDITED_MODELS.items())


def audit_events():
    """Queryable audit history without exposing the host model as plugin API."""

    return AuditLog.objects.all()


def record_event(
    *,
    action: str,
    obj=None,
    type_label: str | None = None,
    message: str = "",
    metadata: dict | None = None,
    user=None,
    required: bool = False,
) -> AuditLog:
    """Write an audit row, optionally failing the surrounding transaction.

    Signals and informational events remain best-effort so an unavailable
    audit sink cannot make unrelated reads or housekeeping fail. Mutating
    application services can pass ``required=True`` when committed state
    without its audit record would violate their contract.
    """

    user = user or get_current_user()
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None

    object_type = type_label or (
        obj.__class__.__name__ if obj is not None else ""
    )
    object_id = str(getattr(obj, "pk", "")) if obj is not None else ""
    object_repr = ""
    if obj is not None:
        try:
            object_repr = str(obj)[:200]
        except Exception:  # noqa: BLE001 - defensive
            object_repr = ""

    context = _operation_context.get()
    event_metadata = dict(metadata or {})
    if context is not None:
        event_metadata = {
            "interface": context.interface,
            "actor": context.actor,
            "operation": context.operation,
            **event_metadata,
        }

    try:
        return AuditLog.objects.create(
            user=user,
            action=action,
            object_type=object_type,
            object_id=object_id,
            object_repr=object_repr,
            operation_id=context.operation_id if context is not None else "",
            message=message,
            metadata=event_metadata,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write AuditLog entry")
        if required:
            raise
        return None  # type: ignore[return-value]


def record_operation(
    operation: str,
    message: str,
    *,
    action: str = AuditLog.Action.UPDATED,
    metadata: dict | None = None,
    required: bool = False,
) -> AuditLog:
    """Record one summary event for a bulk or multi-row operation."""

    return record_event(
        action=action,
        type_label=operation,
        message=message,
        metadata=metadata,
        required=required,
    )
