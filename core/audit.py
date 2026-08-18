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

from django.db.models.signals import post_delete, post_init, post_save
from django.dispatch import receiver

from .facets import as_metadata as facet_metadata
from .middleware import get_current_user
from .models import AuditLog


logger = logging.getLogger("severino.audit")

_AUDITED_MODELS: dict[type, str] = {}
_operation_context: ContextVar["OperationContext | None"] = ContextVar(
    "hq_operation_context", default=None
)



# What a value looks like in the log. Audit rows are JSON, so a Decimal, date
# or UUID has to be a string first -- left as it is, the row fails to write and
# `record_event` swallows it.
REDACTED = "«redacted»"
VALUE_CHARS = 200


def _readable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value if not isinstance(value, str) else value[:VALUE_CHARS]
    return str(value)[:VALUE_CHARS]


def _snapshot(instance) -> dict:
    """The instance's concrete fields, as they stand.

    Only fields actually loaded: touching a deferred one fires a query per
    field per instance, which would turn a list page into hundreds of queries.
    """
    loaded = instance.__dict__
    return {
        field.attname: _readable(loaded[field.attname])
        for field in instance._meta.concrete_fields
        # `auto_now` fields move on every save by definition, so including
        # them would mean no save is ever a no-op and every diff carries a
        # line saying the clock advanced.
        if field.attname in loaded and not getattr(field, "auto_now", False)
    }


def _changes(before: dict | None, after: dict, secret: frozenset) -> dict:
    """Which fields moved, and what they moved between.

    `before` is None for an instance constructed rather than read -- nothing
    is known about the previous state, so nothing is claimed about it.
    """
    if before is None:
        return {}
    changed = {}
    for name, new in after.items():
        if name not in before or before[name] == new:
            continue
        if name in secret:
            changed[name] = [REDACTED, REDACTED]
        else:
            changed[name] = [before[name], new]
    return changed


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


def register_audit(model, type_label: str, *, redact: Iterable[str] = ()) -> None:
    """Register a model so create/update/delete events land in the audit log.

    `redact` names fields whose values must never reach the log. The field is
    still reported as having changed -- that a token was rotated is worth
    logging -- but the values are replaced.
    """

    if model in _AUDITED_MODELS:
        return
    _AUDITED_MODELS[model] = type_label
    secret = frozenset(redact)

    @receiver(post_init, sender=model, weak=False)
    def _on_init(sender, instance, **kwargs):
        # What the row looked like when it was read. Taken here rather than
        # re-read on save, which would put a second query on every write.
        instance._audit_snapshot = _snapshot(instance)

    @receiver(post_save, sender=model, weak=False)
    def _on_save(sender, instance, created, **kwargs):
        changes = {}
        if not created:
            changes = _changes(
                getattr(instance, "_audit_snapshot", None),
                _snapshot(instance),
                secret,
            )
            # A save that changed nothing is not an event. Django writes
            # every field on every `save()`, so an unchanged re-submit would
            # otherwise leave a row saying "Updated" and meaning nothing.
            if not changes:
                return
        record_event(
            action=AuditLog.Action.CREATED if created else AuditLog.Action.UPDATED,
            obj=instance,
            type_label=type_label,
            metadata={"changes": changes} if changes else None,
        )
        # Re-armed for the next save in the same request: without this, a
        # second save would re-report the first one's changes.
        instance._audit_snapshot = _snapshot(instance)

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
    facets=(),
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
    # Facets first, so an explicit `metadata` key still wins.
    event_metadata = {**facet_metadata(facets), **(metadata or {})}
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
