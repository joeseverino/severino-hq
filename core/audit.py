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
from datetime import timedelta
from typing import Any, Callable, Iterable

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
_connection_context: ContextVar[str] = ContextVar("hq_audit_connection", default="")

# The object type of an event about one connection.
CONNECTION_AUDIT_TYPE = "Connection"

# Machine records of HQ looking, as (action, object type). The only events
# `prune_routine` removes, after SEVERINO_AUDIT_ROUTINE_DAYS.
ROUTINE_EVENTS: frozenset[tuple[str, str]] = frozenset(
    {(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE)}
)
# The security record: people, access and change. Never pruned.
SECURITY_ACTIONS: frozenset[str] = frozenset(
    {
        AuditLog.Action.CREATED,
        AuditLog.Action.UPDATED,
        AuditLog.Action.DELETED,
        AuditLog.Action.LOGIN,
        AuditLog.Action.LOGOUT,
        AuditLog.Action.LOGIN_FAILED,
        AuditLog.Action.UPLOADED,
        AuditLog.Action.EXPORTED,
        AuditLog.Action.IMPORTED,
        AuditLog.Action.FAILED,
        AuditLog.Action.SETTINGS_CHANGED,
        AuditLog.Action.VIEWED,
        AuditLog.Action.DENIED,
    }
)
if {action for action, _ in ROUTINE_EVENTS} & SECURITY_ACTIONS:
    raise ValueError("A security action is declared routine.")



# What a value looks like in the log. Audit rows are JSON, so a Decimal, date
# or UUID has to be a string first: left as it is, the row fails to write and
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

    `before` is None for an instance constructed rather than read: nothing
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


@contextmanager
def audit_connection(connection_ref: str):
    """Name the connection the events in this block went through."""

    token = _connection_context.set(str(connection_ref or "").strip())
    try:
        yield
    finally:
        _connection_context.reset(token)


def register_audit(
    model,
    type_label: str,
    *,
    redact: Iterable[str] = (),
    observation: Iterable[str] = (),
    connection: Callable[[Any], str] | None = None,
) -> None:
    """Register a model so create/update/delete events land in the audit log.

    `redact` names fields whose values must never reach the log. The field is
    still reported as having changed (that a token was rotated is worth
    logging) but the values are replaced.

    `connection` returns the connection_ref an instance's work goes through.
    """

    if model in _AUDITED_MODELS:
        return
    _AUDITED_MODELS[model] = type_label
    secret = frozenset(redact)
    # Fields that record HQ *looking*, not the world changing. A sweep confirms
    # hundreds of declarations per pass and stamps the time on each; audited as
    # changes that is a row per resource per sweep saying "checked, still fine",
    # burying every real event. The stamp is still written and still read by the
    # staleness rules; it is just not an event on its own.
    looked = frozenset(observation)

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
            # Nor is a save that only recorded having looked. Dropped after the
            # diff rather than before it, so the timestamp still appears beside
            # a real change and only disappears when it is the whole story.
            if looked and changes and set(changes) <= looked:
                instance._audit_snapshot = _snapshot(instance)
                return
            if not changes:
                return
        record_event(
            action=AuditLog.Action.CREATED if created else AuditLog.Action.UPDATED,
            obj=instance,
            type_label=type_label,
            metadata={"changes": changes} if changes else None,
            connection=_connection_of(connection, instance),
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
            connection=_connection_of(connection, instance),
        )


def _connection_of(extract: Callable[[Any], str] | None, instance) -> str:
    if extract is None:
        return ""
    try:
        return str(extract(instance) or "")
    except Exception:  # noqa: BLE001 - attribution never blocks the event
        logger.exception("Could not name the connection of an audited %s", type(instance))
        return ""


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
    connection: str = "",
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
            connection=(str(connection or "").strip() or _connection_context.get())[:160],
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
    facets=(),
    required: bool = False,
) -> AuditLog:
    """Record one summary event for a bulk or multi-row operation.

    ``facets`` is the typed form and the one to prefer: a bulk operation is
    exactly the event whose "how much changed" should be comparable with every
    other one. ``metadata`` remains for facts no facet covers, and still wins
    on a key collision, as it does in ``record_event``.
    """

    return record_event(
        action=action,
        type_label=operation,
        message=message,
        metadata=metadata,
        facets=facets,
        required=required,
    )


def last_activity(connections: Iterable[str]) -> dict[str, AuditLog]:
    """The latest audit event per named connection, in one query however many."""

    from django.db.models import Max

    refs = {ref for ref in connections if ref}
    if not refs:
        return {}
    latest = (
        AuditLog.objects.filter(connection__in=refs)
        .values("connection")
        .annotate(last=Max("pk"))
        .values("last")
    )
    return {
        event.connection: event
        for event in AuditLog.objects.filter(pk__in=latest).only(
            "pk", "action", "object_type", "object_repr", "message", "connection", "created_at"
        )
    }


def prune_routine(*, days: int, check_only: bool = False, batch: int = 1000) -> int:
    """Delete routine machine events older than ``days``; return how many.

    Only ROUTINE_EVENTS with no user are eligible. Deleted in batches.
    """

    from django.db.models import Q
    from django.utils import timezone

    if days < 1:
        raise ValueError("Routine audit retention must be at least one day.")
    kinds = Q(pk__in=[])
    for action, object_type in ROUTINE_EVENTS:
        kinds |= Q(action=action, object_type=object_type)
    eligible = AuditLog.objects.filter(
        kinds,
        user__isnull=True,
        created_at__lt=timezone.now() - timedelta(days=days),
    )
    if check_only:
        return eligible.count()
    deleted = 0
    while True:
        ids = list(eligible.values_list("pk", flat=True)[:batch])
        if not ids:
            return deleted
        deleted += AuditLog.objects.filter(pk__in=ids).delete()[0]
