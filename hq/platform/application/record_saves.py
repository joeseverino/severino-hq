"""Saving a record held as its caller last read it, and the records it links to.

An asset, an expense, a writeup and a document are written the same way: the
domain's write permission, an audited create or update, the record held as last
read, its links found by the keys the command names them by, and ``full_clean``.
Each domain declares its ``save`` with ``linked_saver``, saying only what is its
own; any other write that changes a record the caller read holds it with ``held``.
"""

from annotationlib import Format, call_annotate_function
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import date
from decimal import Decimal
from functools import cache
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
    Required,
    TypeAliasType,
    TypedDict,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Model

from .answer_fields import CallMessage
from .command_fields import Names, names_of
from .describe import Describe
from .domains import records_of
from .host_sections import load
from .integration_specs import TARGET_KINDS, Kept, keeps
from .json_schemas import Written, as_written
from .record_declaration import Records
from .record_errors import RecordConflict, RecordNotFound, Sentence
from .security import Principal
lazy from .projection import iso
lazy from .resources import masked_kind, resource_registry
lazy from .ui import ended

Save = Callable[..., dict[str, Any]]

# What a save says of the record it wrote, for every kind of record alike.
type Created = Annotated[bool, Describe("Whether the call created the record, rather than changing one HQ held.")]


@cache
def saved_type(resource: str) -> Any:
    """What a save of ``resource`` answers: the record as a read of one
    answers it, under the domain's noun, and whether the call created it.

    Read off the resource's declared record type, so a domain declares the
    shape of its record once and its save answers in it.
    """

    records = records_of(resource)
    spec = resource_registry()[resource]
    record = masked_kind(spec, listed=False) or masked_kind(spec, listed=True)
    saved: Any = TypedDict(  # type: ignore[operator]
        _saved_name(records),
        {"ok": Literal[True], "created": Created, records.noun: record, "message": CallMessage},
    )
    saved.__doc__ = f"The {records.title or records.noun} as saved, and whether the call created it."
    saved.__module__ = __name__
    return saved


def _saved_name(records: Records) -> str:
    return "".join(part.title() for part in records.noun.split("_")) + "Saved"


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

    Without ``expected_updated_at`` the record is taken as it is now.
    """

    record = found(model, refusals, lookup, locked=True)
    if expected_updated_at and record.updated_at.isoformat() != expected_updated_at:
        raise refusals.conflict()
    return record


def found(model: type[Model], refusals: Held, lookup: Mapping[str, Any], *, locked: bool = False) -> Any:
    """The record ``lookup`` names, ``locked`` for this transaction or as it is
    now; a lookup value the key cannot hold names no record."""

    rows = model._default_manager.select_for_update() if locked else model._default_manager
    try:
        return rows.get(**lookup)
    except (ObjectDoesNotExist, ValidationError, ValueError) as exc:
        raise refusals.not_found() from exc


def as_sent(record: Model, command: type, keys: Mapping[str, str]) -> dict[str, Any]:
    """``record``'s value for each field of ``command``, as a caller sends it:
    a field that names other records holds each one's key, which ``keys`` names."""

    values: dict[str, Any] = {}
    for name in (field.name for field in fields(command)):
        field = record._meta.get_field(name)
        if name not in keys:
            values[name] = field.value_from_object(record)
        elif field.many_to_many:
            values[name] = tuple(getattr(linked, keys[name]) for linked in getattr(record, name).all())
        else:
            linked = getattr(record, name)
            values[name] = None if linked is None else getattr(linked, keys[name])
    return values


def row(record: Any, shape: Any, **given: Any) -> Any:
    """``record``, a model or any object, read as ``shape``, the TypedDict that
    declares the answer, each field as its type says.

    A field the record does not hold under its name is ``given``; a field the
    shape does not require is left out unless given.
    """

    values: dict[str, Any] = {}
    for name, names, read in _row_fields(shape):
        if name in given:
            values[name] = read(given[name])
        elif name not in shape.__required_keys__:
            continue
        elif names is None:
            values[name] = read(getattr(record, name))
        else:
            values[name] = _linked(record, name, names)
    return values


def _linked(record: Model, name: str, names: Names) -> Any:
    field = record._meta.get_field(name)
    key = names.key or records_of(names.resource).lookup
    if key == field.target_field.name:
        return getattr(record, field.attname)
    linked = getattr(record, name)
    # Only a doc says whether it may be shared.
    shared = linked is not None and getattr(linked, "is_safe_for_ai_export", True)
    return getattr(linked, key) if shared else None


@cache
def _row_fields(shape: type) -> tuple[tuple[str, Names | None, Callable[[Any], Any]], ...]:
    hints = get_type_hints(shape, include_extras=True)
    return tuple((name, names_of(hint), _reader(hint)) for name, hint in hints.items())


def _reader(hint: Any) -> Callable[[Any], Any]:
    """How a field of type ``hint`` is read from the value its record holds;
    a part already held as a mapping, as a JSON field holds one, is kept."""

    if isinstance(hint, TypeAliasType):
        return _reader(hint.__value__)
    origin, args = get_origin(hint), get_args(hint)
    if origin is Annotated and any(isinstance(mark, Written) for mark in args[1:]):
        return lambda value: value if isinstance(value, Mapping) else as_written(value)
    if origin is Annotated:
        return _reader(args[0])
    if origin in (NotRequired, Required):
        return _reader(args[0])
    if origin is UnionType and NoneType in args and len(args) == 2:
        read = _reader(next(arg for arg in args if arg is not NoneType))
        return lambda value: None if value is None or value == "" else read(value)
    if origin is list:
        each = _reader(args[0])
        return lambda value: [each(item) for item in value]
    if is_typeddict(hint):
        return lambda value: value if isinstance(value, Mapping) else row(value, hint)
    return _plain


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, date):
        return iso(value)
    return value


def written(command: Any, sent: frozenset[str] | None) -> dict[str, Any]:
    """The values a write sets on its record: the fields its caller ``sent``,
    or every field when ``sent`` is None (a create, or a caller that built the
    whole command). A field left out keeps what the record holds, since the
    command holds only its default there.
    """

    values = asdict(command)
    return values if sent is None else {name: value for name, value in values.items() if name in sent}


def linked_saver(
    resource: str,
    serialize: Callable[[Any], dict[str, Any]],
    *,
    refusals: Held,
    links: Mapping[str, Link],
) -> Save:
    """The create-or-update service of ``resource``, in the host call contract.

    ``links`` maps each command field that names other records to how they are
    named; the model's field says whether it holds one record or a set. An
    update writes only the fields its caller ``sent`` (``written``) and keeps
    the rest (``Kept``). The answer holds the record, serialized, under the
    domain's noun.
    """

    records = records_of(resource)

    def stored(current: Any) -> dict[str, Any]:
        record = found(load(records.model), refusals, {records.lookup: current})
        return as_sent(record, load(records.command), {name: link.key for name, link in links.items()})

    @keeps(Kept(stored))
    def save(
        command: Any,
        *,
        principal: Principal,
        expected_updated_at: str | None = None,
        sent: frozenset[str] | None = None,
        current: Any = None,
    ) -> dict[str, Any]:
        with transaction.atomic():
            record = _saved(records, command, principal, current, expected_updated_at, sent, refusals, links)
            done = "created" if current is None else "updated"
            return {
                "ok": True,
                "created": current is None,
                records.noun: serialize(record),
                "message": ended(f"{records.called} “{record}” {done}"),
            }

    save.__name__ = save.__qualname__ = f"save_{records.noun}"
    declared = save.__annotate__

    def annotate(wanted: Format) -> dict[str, Any]:
        # Its answer is named for the record type the registry holds, read
        # only once a value is asked for: a spec is validated by the names alone.
        annotations = dict(call_annotate_function(declared, wanted)) if declared else {}
        annotations["return"] = _saved_name(records) if wanted == Format.STRING else saved_type(resource)
        return annotations

    save.__annotate__ = annotate
    TARGET_KINDS[records.target].binds(save)
    return save


def _saved(
    records: Records,
    command: Any,
    principal: Principal,
    current: Any,
    expected_updated_at: str | None,
    sent: frozenset[str] | None,
    refusals: Held,
    links: Mapping[str, Link],
) -> Any:
    model = load(records.model)
    record = model() if current is None else held(model, refusals, {records.lookup: current}, expected_updated_at)
    values = written(command, sent if current is not None else None)
    sets = {}
    for name, link in links.items():
        if name not in values:
            continue
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
    except (ObjectDoesNotExist, ValidationError, ValueError) as exc:
        raise link.not_found() from exc
