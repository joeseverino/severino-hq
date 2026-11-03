"""
Audit helpers.

Every domain model is registered via ``register_audit(Model, type_label)``.
post_save / post_delete signals then write to AuditLog, attributing the change
to whoever the request being answered signed in (``request_context``).
"""

import json
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from logging import getLogger
from typing import Any, Literal, overload
from uuid import UUID

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models.signals import post_delete, post_init, post_save
from django.dispatch import receiver
lazy from django.contrib.auth.base_user import AbstractBaseUser
lazy from django.db.models import Max, Model, Q, QuerySet
lazy from django.utils import timezone

from hq.platform.application import request_context
from hq.platform.application.payload_words import changed_words
lazy from hq.platform.application.request_identity import signed_in

from . import speculation
from .attribution import OperationContext, bound, operation_context
from .facets import as_metadata as facet_metadata
from .logging import get_request_id
from .models import AuditLog
lazy from .facets import Facet

logger = getLogger("severino.audit")

_AUDITED_MODELS: dict[type, str] = {}
_connection_context: ContextVar[str] = ContextVar("hq_audit_connection", default="")
# A seed's say in the rows it causes: when each happened and who did it.
type Stamp = Callable[[Model | None, str, str], dict[str, Any] | None]
_stamp_context: ContextVar[Stamp | None] = ContextVar("hq_audit_stamp", default=None)

# The object type of an event about one connection.
CONNECTION_AUDIT_TYPE = "Connection"

# Machine records of HQ looking, as (action, object type). The only events
# `prune_routine` removes, after SEVERINO_AUDIT_ROUTINE_DAYS.
ROUTINE_EVENTS: frozenset[tuple[str, str]] = frozenset(
    {(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE), (AuditLog.Action.OBSERVED, "Background job")}
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
        AuditLog.Action.DONE,
        AuditLog.Action.FAILED,
        AuditLog.Action.SETTINGS_CHANGED,
        AuditLog.Action.VIEWED,
        AuditLog.Action.DENIED,
    }
)
if {action for action, _ in ROUTINE_EVENTS} & SECURITY_ACTIONS:
    raise ValueError("A security action is listed as routine.")


# What a value looks like in the log. Audit rows are JSON, so a Decimal, date
# or UUID has to be a string first: left as it is, the row fails to write and
# `record_event` swallows it.
REDACTED = "«redacted»"
VALUE_CHARS = 200


def _readable(value):
    """A value as the diff compares it. A record or list is copied whole as
    JSON, so a change anywhere in it is seen, and said in words only once it is
    known to have moved.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value if not isinstance(value, str) else value[:VALUE_CHARS]
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.loads(json.dumps(value, cls=DjangoJSONEncoder))
        except TypeError, ValueError:
            pass
    return str(value)[:VALUE_CHARS]


def _cut(words: str) -> str:
    return words if len(words) <= VALUE_CHARS else f"{words[: VALUE_CHARS - 1]}…"


def _inner_changes(before, after) -> dict[str, list[str]]:
    """A record or list that moved, as each value in it that moved, in words,
    by where it is inside the field ("" for the field as a whole).
    """
    return {row.label: [_cut(row.before), _cut(row.after)] for row in changed_words(before, after)}


# Types whose values cannot change in place. Holding one is the same as
# holding what it looked like, so its conversion waits for a save. Matched on
# the exact type: a subclass may carry state of its own.
_IMMUTABLE: frozenset[type] = frozenset(
    {type(None), bool, int, float, str, Decimal, date, datetime, time, timedelta, UUID}
)

# Names the audit receivers of one model, so they can be found on the signal.
AUDIT_DISPATCH_UID = "hq.audit"
_SIGNALS = (post_init, post_save, post_delete)


def _tracked(model) -> tuple[str, ...]:
    """The attribute names a model's audit diff compares, in field order.

    `auto_now` fields move on every save by definition, so including them
    would mean no save is ever a no-op and every diff carries a line saying
    the clock advanced.
    """
    return tuple(field.attname for field in model._meta.concrete_fields if not getattr(field, "auto_now", False))


def _capture(instance, names: tuple[str, ...]) -> dict:
    """What a later save is compared against, at the cost a read can afford.

    This runs for every instance a query returns, and almost none are saved.
    An immutable value is held as it is; anything else (a JSON dict or list, a
    file) is made readable now, since it can change in place. Only loaded fields
    are read: a deferred one would fire a query per field per instance.
    """
    loaded = instance.__dict__
    held = {}
    for name in names:
        if name in loaded:
            value = loaded[name]
            held[name] = value if type(value) in _IMMUTABLE else _readable(value)
    return held


def _snapshot(instance, names: tuple[str, ...]) -> dict:
    """The instance's loaded concrete fields, as the log shows them."""
    loaded = instance.__dict__
    return {name: _readable(loaded[name]) for name in names if name in loaded}


def _shown(held: dict | None) -> dict | None:
    """A capture, as the log shows it."""
    if held is None:
        return None
    return {name: _readable(value) for name, value in held.items()}


def _changes(before: dict | None, after: dict, secret: frozenset) -> dict:
    """Which fields moved, and what they moved between: a pair for a plain
    value, and for a record or list the pair of each value in it that moved,
    in words, by where it is.

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
        elif isinstance(before[name], (dict, list)) or isinstance(new, (dict, list)):
            if inner := _inner_changes(before[name], new):
                changed[name] = inner
        else:
            changed[name] = [before[name], new]
    return changed


# Who a row names when its request's caller never signed in.
ANONYMOUS = "anonymous"


class Unattributed(RuntimeError):
    """An audit row written with nothing bound to say who acted: a door that did not bind."""


def _bound(doing: str) -> OperationContext:
    context = bound()
    if context is None:
        raise Unattributed(
            f"{doing} with no operation bound. Every door binds one where it starts "
            "(hq.platform.core.attribution); work outside a door binds its own with audit_operation."
        )
    return context


@contextmanager
def audit_operation(*, operation: str, principal: object | None = None, operation_id: str = "") -> Iterator[None]:
    """Attribute an operation to the principal that made it: the one way a
    host service or a plugin binds a principal's interface and actor.

    Without a principal the operation is the bound caller's, and with nobody
    bound it is refused, so no operation is written down as nobody's.
    """

    # A plugin hands whatever principal it holds, or none.
    outer = _bound(f"The operation {operation!r} began") if principal is None else bound()
    with operation_context(
        interface=getattr(principal, "interface", None) or (outer.interface if outer else ""),
        actor=getattr(principal, "actor", None) or (outer.actor if outer else ""),
        operation=operation,
        operation_id=operation_id,
    ):
        yield


@contextmanager
def audit_connection(connection_ref: str) -> Iterator[None]:
    """Name the connection the events in this block went through."""

    with _connection_context.set(str(connection_ref or "").strip()):
        yield


@contextmanager
def stamped(stamp: Stamp) -> Iterator[None]:
    """Let ``stamp`` date and attribute each event recorded in the block.

    For a seed, whose records are all written in one moment: the log is
    append-only, so an event is written with its time and author or not at
    all. ``stamp(obj, action, object_type)`` answers ``created_at``, ``user``,
    ``metadata`` to merge in or the ``operation_id`` of the request that made
    it, or None to leave the event as it is.
    """

    with _stamp_context.set(stamp):
        yield


def audited_labels() -> dict[type, str]:
    """Each registered model and the label its audit rows are stored under."""

    return dict(_AUDITED_MODELS)


@contextmanager
def withdrawn(*models: type[Model]) -> Iterator[dict[type, str]]:
    """Take `models` out of audit for the block, yielding each one's label.

    For a test that audits a model its own way: inside the block the model has
    no audit receiver and may be registered again, and after it the registry
    and the model signals' receivers are as they were.
    """

    unknown = [model.__name__ for model in models if model not in _AUDITED_MODELS]
    if unknown:
        raise LookupError(f"Not audited: {', '.join(unknown)}.")
    registry = dict(_AUDITED_MODELS)
    receivers = {signal: list(signal.receivers) for signal in _SIGNALS}
    labels = {model: _AUDITED_MODELS.pop(model) for model in models}
    for signal in _SIGNALS:
        for model in models:
            signal.disconnect(sender=model, dispatch_uid=AUDIT_DISPATCH_UID)
    try:
        yield labels
    finally:
        _AUDITED_MODELS.clear()
        _AUDITED_MODELS.update(registry)
        for signal, kept in receivers.items():
            with signal.lock:
                signal.receivers = kept
                signal.sender_receivers_cache.clear()


def register_audit(
    model: type[Model],
    type_label: str,
    *,
    redact: Iterable[str] = (),
    observation: Iterable[str] = (),
    connection: Callable[[Any], str] | None = None,
    redact_for: Callable[[Any], Iterable[str]] | None = None,
) -> None:
    """Register a model so create/update/delete events land in the audit log.

    `redact` names fields whose values never reach the log, though the change
    is still reported (a token rotated is worth logging). `redact_for` names
    more for one instance, where it depends on the row (a declaration of a
    withheld kind). `connection` returns the connection_ref an instance's work
    goes through; `observation` names fields that only record HQ looking.
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

    names = _tracked(model)

    @receiver(post_init, sender=model, weak=False, dispatch_uid=AUDIT_DISPATCH_UID)
    def _on_init(instance, **kwargs):
        # What the row looked like when it was read. Taken here rather than
        # re-read on save, which would put a second query on every write.
        instance._audit_snapshot = _capture(instance, names)  # noqa: SLF001 - audit's own attribute, kept on each instance it watches

    @receiver(post_save, sender=model, weak=False, dispatch_uid=AUDIT_DISPATCH_UID)
    def _on_save(instance, created, **kwargs):
        changes = {}
        if not created:
            changes = _changes(
                # One read before its model was registered carries no capture, and claims no change.
                _shown(getattr(instance, "_audit_snapshot", None)),
                _snapshot(instance, names),
                secret | frozenset(redact_for(instance) if redact_for else ()),
            )
            # A save that changed nothing is not an event. Django writes
            # every field on every `save()`, so an unchanged re-submit would
            # otherwise leave a row saying "Updated" and meaning nothing.
            # Nor is a save that only recorded having looked. Dropped after the
            # diff rather than before it, so the timestamp still appears beside
            # a real change and only disappears when it is the whole story.
            if looked and changes and set(changes) <= looked:
                instance._audit_snapshot = _capture(instance, names)  # noqa: SLF001 - audit's own attribute, kept on each instance it watches
                return
            if not changes:
                return
        record_event(
            action=AuditLog.Action.CREATED if created else AuditLog.Action.UPDATED,
            obj=instance,
            type_label=type_label,
            # What this save meant, when the one saving it knows; once.
            message=str(instance.__dict__.pop("audit_said", "") or ""),
            metadata={"changes": changes} if changes else None,
            connection=_connection_of(connection, instance),
        )
        # Re-armed for the next save in the same request: without this, a
        # second save would re-report the first one's changes.
        instance._audit_snapshot = _capture(instance, names)  # noqa: SLF001 - audit's own attribute, kept on each instance it watches

    @receiver(post_delete, sender=model, weak=False, dispatch_uid=AUDIT_DISPATCH_UID)
    def _on_delete(instance, **kwargs):
        # What going away meant, when the one removing it knows: a read request
        # answered, a declaration forgotten. "Deleted" otherwise.
        record_event(
            action=AuditLog.Action.DELETED,
            obj=instance,
            type_label=type_label,
            message=str(getattr(instance, "audit_gone", "") or ""),
            connection=_connection_of(connection, instance),
        )


def _connection_of(extract: Callable[[Any], str] | None, instance) -> str:
    if extract is None:
        return ""
    try:
        return str(extract(instance) or "")
    except Exception:  # attribution never blocks the event
        logger.exception("Could not name the connection of an audited %s", type(instance))
        return ""


def audit_events() -> QuerySet[AuditLog]:
    """Queryable audit history without exposing the host model as plugin API."""

    return AuditLog.objects.all()


# A required event raises rather than return nothing, so its caller is told it
# has a row; any other may be skipped by a failed write.
@overload
def record_event(
    *,
    action: str,
    obj: Model | None = None,
    type_label: str | None = None,
    message: str = "",
    metadata: dict | None = None,
    facets: Iterable[Facet] = (),
    user: AbstractBaseUser | None = None,
    required: Literal[True],
    connection: str = "",
) -> AuditLog:
    pass


@overload
def record_event(
    *,
    action: str,
    obj: Model | None = None,
    type_label: str | None = None,
    message: str = "",
    metadata: dict | None = None,
    facets: Iterable[Facet] = (),
    user: AbstractBaseUser | None = None,
    required: bool = False,
    connection: str = "",
) -> AuditLog | None:
    pass


def record_event(
    *,
    action: str,
    obj: Model | None = None,
    type_label: str | None = None,
    message: str = "",
    metadata: dict | None = None,
    facets: Iterable[Facet] = (),
    user: AbstractBaseUser | None = None,
    required: bool = False,
    connection: str = "",
) -> AuditLog | None:
    """Write an audit row, optionally failing the surrounding transaction.

    Signals and informational events remain best-effort so an unavailable
    audit sink cannot make unrelated reads or housekeeping fail. Mutating
    application services can pass ``required=True`` when committed state
    without its audit record would violate their contract.
    """

    # A page fetched on a guess does nothing worth recording, so it does not
    # get as far as recording it.
    speculation.refuse("it does something the audit log records")
    # Raised before the best-effort write below, so a path that reaches here
    # unbound fails where it runs rather than leaving a row nobody wrote.
    context = _bound(f"A {action} row was recorded")
    # The request is what a context variable holds, never its lazy user:
    # asgiref compares a variable's values when it restores a context
    # (``asgiref.sync._restore_context``), and comparing the lazy user would
    # load it from the session on the event loop's thread.
    user = user or signed_in(request_context.current())
    if user is not None and not user.is_authenticated:
        user = None

    object_type = type_label or (obj.__class__.__name__ if obj is not None else "")
    object_id = str(obj.pk) if obj is not None else ""
    object_repr = ""
    if obj is not None:
        try:
            object_repr = str(obj)[:200]
        except Exception:  # noqa: BLE001 - an object's own __str__ may raise
            object_repr = ""

    stamp = _stamp_context.get()
    stamps = (stamp(obj, action, object_type) if stamp is not None else None) or {}
    user = stamps.get("user", user)
    # Facets first, so an explicit `metadata` key still wins.
    event_metadata = {
        "interface": context.interface,
        "actor": _actor(context, user, stamped_user="user" in stamps),
        "operation": context.operation,
        **facet_metadata(facets),
        **(metadata or {}),
        **stamps.get("metadata", {}),
    }

    try:
        row = AuditLog.objects.create(
            user=user,
            action=action,
            object_type=object_type,
            object_id=object_id,
            object_repr=object_repr,
            operation_id=stamps.get("operation_id", context.operation_id),
            connection=(str(connection or "").strip() or _connection_context.get())[:160],
            message=message,
            metadata=event_metadata,
            **({"created_at": stamps["created_at"]} if "created_at" in stamps else {}),
        )
    except Exception:
        logger.exception("Failed to write AuditLog entry")
        if required:
            raise
        return None
    emit_line("audit.recorded", _line(row))
    return row


def _actor(context: OperationContext, user: AbstractBaseUser | None, *, stamped_user: bool) -> str:
    """Who a row says acted, in its own text, which outlives the account the
    user column points at: a seed's stamped author, else the operation's actor,
    else whoever the request had signed in when the row was written."""

    if stamped_user and user is not None:
        return user.get_username()
    if context.actor:
        return context.actor
    return user.get_username() if user is not None else ANONYMOUS


def _line(row: AuditLog) -> dict[str, Any]:
    """A stored row as its log line carries it: what was written, redacted as it was."""

    metadata = row.metadata or {}
    return {
        "id": row.pk,
        "created_at": row.created_at.isoformat(),
        "action": row.action,
        "user_id": row.user_id,
        "actor": metadata.get("actor"),
        "interface": metadata.get("interface"),
        "object_type": row.object_type,
        "object_id": row.object_id,
        "object_repr": row.object_repr,
        "operation_id": row.operation_id,
        "connection": row.connection,
        "message": row.message,
        "metadata": metadata,
    }


def emit_line(event: str, line: dict[str, Any]) -> None:
    """Write ``line`` as one JSON line on the audit logger once the write it
    describes commits, and never for one rolled back.

    The copy of the log outside HQ: what a reader of the container's output,
    or a shipper of it, holds even if the database is lost or rewritten.
    """

    line = {**line, "request_id": get_request_id()}
    transaction.on_commit(
        lambda: logger.info("%s %s", event, line["id"], extra={"event": event, "audit": line}),
        robust=True,
    )


@overload
def record_operation(
    operation: str,
    message: str,
    *,
    action: str = AuditLog.Action.UPDATED,
    metadata: dict | None = None,
    facets: Iterable[Facet] = (),
    required: Literal[True],
) -> AuditLog:
    pass


@overload
def record_operation(
    operation: str,
    message: str,
    *,
    action: str = AuditLog.Action.UPDATED,
    metadata: dict | None = None,
    facets: Iterable[Facet] = (),
    required: bool = False,
) -> AuditLog | None:
    pass


def record_operation(
    operation: str,
    message: str,
    *,
    action: str = AuditLog.Action.UPDATED,
    metadata: dict | None = None,
    facets: Iterable[Facet] = (),
    required: bool = False,
) -> AuditLog | None:
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

    refs = {ref for ref in connections if ref}
    if not refs:
        return {}
    latest = AuditLog.objects.filter(connection__in=refs).values("connection").annotate(last=Max("pk")).values("last")
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
