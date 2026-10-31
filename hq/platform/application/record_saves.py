"""Saving a record held as its caller last read it, and the records it links to.

An asset, an expense, a writeup and a document are written the same way: the
domain's write permission, an audited create or update, the record held as last
read, its links found by the keys the command names them by, and ``full_clean``.
Each domain declares its ``save`` with ``linked_saver``, saying only what is its
own; any other write that changes a record the caller read holds it with ``held``.
"""

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Model

from hq.platform.core.audit import operation_context

from .domains import Records, load, records_of
from .integration_specs import TARGET_KINDS
from .record_errors import RecordConflict, RecordNotFound, Sentence
from .security import Principal

Save = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Held:
    """What a write says when the record it changes is gone, or changed since it was read.

    Each is a fixed sentence written where the record is declared, so a reader
    is told which kind of record it was and nothing the request sent.
    """

    missing: Sentence
    stale: Sentence

    def not_found(self) -> RecordNotFound:
        return RecordNotFound(self.missing)

    def conflict(self) -> RecordConflict:
        return RecordConflict(self.stale)


@dataclass(frozen=True, slots=True)
class Link:
    """A command field that names other records: the field they are named by,
    and what a write says when one it names is not there."""

    key: str
    missing: Sentence

    def not_found(self) -> RecordNotFound:
        return RecordNotFound(self.missing)


def held(model: type[Model], refusals: Held, lookup: Mapping[str, Any], expected_updated_at: str | None) -> Any:
    """The record ``lookup`` names, locked for this transaction, as the caller last read it.

    A lookup value the key cannot hold (a word for a number) names no record.
    Without ``expected_updated_at`` the record is taken as it is now.
    """

    try:
        record = model._default_manager.select_for_update().get(**lookup)
    except (ObjectDoesNotExist, ValueError) as exc:
        raise refusals.not_found() from exc
    if expected_updated_at and record.updated_at.isoformat() != expected_updated_at:
        raise refusals.conflict()
    return record


def linked_saver(
    resource: str,
    serialize: Callable[[Any], dict[str, Any]],
    *,
    refusals: Held,
    links: Mapping[str, Link],
) -> Save:
    """The create-or-update service of ``resource``, in the host call contract.

    ``links`` maps each command field that names other records to how they are
    named; the model's field says whether it holds one record or a set. The
    answer holds the record, serialized, under the domain's noun.
    """

    records = records_of(resource)
    keyword = TARGET_KINDS[records.target].keyword

    def save(
        command: Any,
        *,
        principal: Principal,
        expected_updated_at: str | None = None,
        **target: Any,
    ) -> dict[str, Any]:
        if set(target) - {keyword}:
            raise TypeError(f"{records.noun} is saved by {keyword}.")
        current = target.get(keyword)
        with transaction.atomic():
            record = _saved(records, command, principal, current, expected_updated_at, refusals, links)
            return {"ok": True, "created": current is None, records.noun: serialize(record)}

    save.__name__ = save.__qualname__ = f"save_{records.noun}"
    return save


def _saved(
    records: Records,
    command: Any,
    principal: Principal,
    current: Any,
    expected_updated_at: str | None,
    refusals: Held,
    links: Mapping[str, Link],
) -> Any:
    principal.require(records.write)
    model = load(records.model)
    operation = f"{records.noun}.{'create' if current is None else 'update'}"
    with operation_context(interface=principal.interface, actor=principal.actor, operation=operation):
        record = model() if current is None else held(model, refusals, {records.lookup: current}, expected_updated_at)
        values = asdict(command)
        sets = {}
        for name, link in links.items():
            field = model._meta.get_field(name)
            if field.many_to_many:
                sets[name] = _linked_set(field.related_model, link, values.pop(name))
            else:
                values[name] = _linked_one(field.related_model, link, values.pop(name))
        for name, value in values.items():
            setattr(record, name, value)
        record.full_clean()
        record.save()
        for name, found in sets.items():
            getattr(record, name).set(found)
    return record


def _linked_set(model: type[Model], link: Link, values: tuple) -> list[Any]:
    """The records a write links to: every one named, or a refusal."""

    found = list(model._default_manager.filter(**{f"{link.key}__in": values}))
    if set(values) - {getattr(record, link.key) for record in found}:
        raise link.not_found()
    return found


def _linked_one(model: type[Model], link: Link, value: Any) -> Any:
    """The one record a write links to, None when it names none."""

    if value in (None, ""):
        return None
    try:
        return model._default_manager.get(**{link.key: value})
    except ObjectDoesNotExist as exc:
        raise link.not_found() from exc
