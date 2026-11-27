"""The shapes the readings resources answer in: each type of reading's row,
item and record, and each record a type holds, with the query that filters
them by their own fields."""

from collections.abc import Mapping
from dataclasses import dataclass
from difflib import get_close_matches
from enum import Enum
from functools import cache
from types import MappingProxyType, NoneType, UnionType
from typing import Annotated, Any, Literal, TypedDict, Union, get_args, get_origin

from pydantic import BaseModel, Field, ValidationError, ValidationInfo, create_model
from pydantic_core import ErrorDetails, InitErrorDetails, PydanticCustomError, to_jsonable_python

from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.contract import READERS, READING_FACETS
from hq.domains.control_plane.provider_adapters.contracts import REFUSALS

from .answer_fields import AsRead, ObservedAt
from .contracts import Closed
from .credential_sight import SightState
from .describe import Describe, listed
from .input_errors import misnamed
from .reasons import Checked, Reason
from .resources import BoundedQuery, one_of
from .ui import ended


class ReadingRow(TypedDict):
    kind: Annotated[str, Describe("The reading's type, such as `adguard.client`, which a read of one names it by.")]
    label: Annotated[str, Describe("The reading's name, in words.")]
    provider: Annotated[
        str | None, Describe("The service it is read from, such as `adguard`; null where no one connection reads it.")
    ]
    state: Annotated[str, Describe(ended(f"How its last read went: {listed(SightState)}"))]
    records: Annotated[int, Describe("How many records the last read stored.")]
    observed_at: ObservedAt | None
    error: Annotated[str, Describe("Why the last read failed, as the service said; empty when it did not.")]


class Aged(TypedDict):
    """How current a kind's last read is, as ``credential_sight.Standing`` says it."""

    freshness: Annotated[str, Describe("How current its last read is, in one word.")]
    freshness_label: Annotated[str, Describe("How current its last read is, in the words its row shows.")]


class ReadingItem(ReadingRow, Aged):
    provider_label: Annotated[str, Describe("The service's name, in words.")]
    read_by: Annotated[
        str,
        Describe(
            f"Who takes the reading, {listed(READERS)}: a controller through a connection's credential, HQ from "
            "a public registry, or HQ from the requests it serves."
        ),
    ]
    facet: Annotated[
        str, Describe(f"What its records supply a service: {listed(sorted(READING_FACETS))}, or empty for none.")
    ]
    relation: Annotated[
        str,
        Describe(
            "What a record is to what it joins, a short phrase such as `Served by Pages project`; empty for none."
        ),
    ]
    requires: Annotated[list[str], Describe("The permissions the read needs, each as the service names it.")]
    refusal: Annotated[
        str,
        Describe(
            f"Whether the service refused the credential or one permission, {listed(REFUSALS)}; empty when neither."
        ),
    ]
    remedy: Annotated[
        str, Describe("What would let HQ read it, such as the permissions to add; empty when nothing is missing.")
    ]


class ReadingRecord(ReadingItem):
    """One type of reading with the records it holds, as its schema admits them."""

    items: Annotated[
        list[AsRead],
        Describe("The records it holds, each as its type's schema admits it; empty for a type HQ withholds."),
    ]


class RecordItem(TypedDict):
    """One record a reading holds, as its type's schema admits it."""

    kind: Annotated[str, Describe("The reading's type, such as `github.workflow_run`.")]
    record: AsRead


@dataclass(frozen=True, slots=True)
class RecordFilter:
    """What a kind's records are filtered by, derived from its record model:
    each field whose value is one text, number, flag or choice, matched by
    equality, and each that lists such values, matched when it holds one."""

    model: type[BaseModel]
    # The fields a record lists values in, which a filter matches by one of them.
    listed: frozenset[str]
    # What a record holds in a field it was stored without, as JSON.
    defaults: Mapping[str, Any]

    def keeps(self, record: Mapping[str, Any], asked: Mapping[str, Any]) -> bool:
        for name, value in asked.items():
            held = record.get(name, self.defaults.get(name))
            if not (value in (held or ()) if name in self.listed else held == value):
                return False
        return True


def _compared(annotation: Any) -> Any:
    """The type a filter's value is held to for a field of ``annotation``, or
    None for a value no filter compares (an instant, an object)."""

    if isinstance(annotation, UnionType) or get_origin(annotation) is Union:
        kept = [arg for arg in get_args(annotation) if arg is not NoneType]
        return _compared(kept[0]) if len(kept) == 1 else None
    if get_origin(annotation) is Literal or (isinstance(annotation, type) and issubclass(annotation, _SCALARS)):
        return annotation
    return None


_SCALARS = (str, int, float, Enum)
_LISTS = (list, tuple, set, frozenset)


@cache
def record_filter(kind: str) -> RecordFilter:
    """The filter of ``kind``'s records, one optional field for each field of
    its record model a value is compared to."""

    record = OBSERVATIONS[kind].record
    fields: dict[str, Any] = {}
    listed: set[str] = set()
    for name, field in record.model_fields.items():
        typed = _compared(field.annotation)
        if (
            typed is None
            and get_origin(field.annotation) in _LISTS
            and (typed := _compared(get_args(field.annotation)[0]))
        ):
            listed.add(name)
        if typed is not None:
            fields[name] = (typed | None, None)
    defaults = {
        name: to_jsonable_python(field.get_default(call_default_factory=True))
        for name, field in record.model_fields.items()
        if name in fields and not field.is_required()
    }
    model = create_model(f"{record.__name__}Filter", __base__=Closed, **fields)
    return RecordFilter(model, frozenset(listed), MappingProxyType(defaults))


def _filtered(where: dict[str, Any] | None, info: ValidationInfo) -> dict[str, Any] | None:
    if not where:
        return None
    kind = info.data.get("kind")
    if kind is None:
        raise _NEEDS_KIND.refused()
    model = record_filter(kind).model
    try:
        return model.model_validate(where).model_dump(mode="json", exclude_unset=True)
    except ValidationError as exc:
        raise ValidationError.from_exception_data(
            exc.title, [_told(error, model, kind) for error in exc.errors()], hide_input=True
        ) from None


def _told(error: ErrorDetails, model: type[BaseModel], kind: str) -> InitErrorDetails:
    """``error``, a field ``kind``'s records are not filtered by told the one
    nearest by name, else every one they are."""

    told = InitErrorDetails(type=error["type"], loc=error["loc"], input=error["input"])
    if "ctx" in error:
        told["ctx"] = error["ctx"]
    if error["type"] != "extra_forbidden":
        return told
    names = list(model.model_fields)
    nearest = get_close_matches(str(error["loc"][-1]), names, n=1, cutoff=0.5)
    told["type"] = (
        misnamed(nearest[0])
        if nearest
        else PydanticCustomError("unfiltered", _UNFILTERED, {"kind": kind, "names": " ".join(names)})
    )
    return told


# What a filter its kind's records do not take is told: every one they do.
_UNFILTERED = "is not a field {kind} records are filtered by; they are filtered by {names}"
_NEEDS_KIND = Reason("records_need_kind", "needs the kind whose record fields it names")


RecordQuery = create_model(
    "RecordQuery",
    __base__=BoundedQuery,
    __doc__="Every record HQ holds of one type of reading, or of every type, narrowed by the values of their own "
    "fields.",
    kind=(
        one_of(kind for kind, spec in OBSERVATIONS.items() if not spec.withheld) | None,
        Field(default=None, description="Only the records of this type of reading; absent, every type's."),
    ),
    where=(
        Annotated[dict[str, str | float | bool | None] | None, Checked(_filtered)],
        Field(
            default=None,
            description="Only the records whose fields hold these values, each a field of `kind`'s records: a "
            "field that lists values holds the one given, and one a record leaves out holds its default; "
            "absent, every record.",
        ),
    ),
)
