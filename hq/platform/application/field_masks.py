"""Field masks over resource records (AIP-157, AIP-161).

A read names the parts of each record it wants; this module cuts the records
to them and refuses a name no record carries, so every adapter answers a mask
the same way.
"""

import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from .json_schemas import LOCAL, json_schema

Fields = list[str] | tuple[str, ...]
# The one field mask that keeps every field (AIP-157): a list's whole item, past its kind's list fields.
WHOLE_RECORD = "*"
# A field mask (AIP-161): each name keeps its whole value, or the names under
# it keep only those parts. ``None`` is a whole value.
type Mask = dict[str, Mask | None]
_FIELD_PATH = re.compile(r"[^.]+(?:\.[^.]+)*")
# A field path as one item of a comma-separated mask, for a reader that has no
# declared paths to choose from: the whole record, or a key with no comma,
# space or star, dotted.
FIELD_PATH_PATTERN = r"^(?:\*|[^.,*\s]+(?:\.[^.,*\s]+)*)$"


class FieldMaskRefusal(ValueError):
    """A mask the records cannot answer; ``errors`` are Pydantic-shaped, located under ``fields``."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("The field mask names no part of these records.")
        self.errors = errors


@functools.cache
def field_paths(kind: Any) -> tuple[str, ...]:
    """Every field path a record of ``kind`` carries, in declared order: each
    key, and each key of an object or a list of objects under it, dotted.

    What a mask over ``kind``'s records can always name: the API document
    lists them, and ``selected`` answers each even where no record has it.
    """

    schema = json_schema(kind, mode="serialization")
    definitions = schema.get("$defs", {})
    found: dict[str, None] = {}

    def walk(value: Mapping[str, Any], within: tuple[str, ...], seen: frozenset[str]) -> None:
        ref = value.get("$ref", "")
        if ref.startswith(LOCAL):
            name = ref.removeprefix(LOCAL)
            if name in seen:
                return
            seen, value = seen | {name}, {**definitions[name], **value}
        for branch in (*value.get("anyOf", ()), *value.get("oneOf", ()), *value.get("allOf", ())):
            walk(branch, within, seen)
        if isinstance(items := value.get("items"), Mapping):
            walk(items, within, seen)
        for key, field in value.get("properties", {}).items():
            if _FIELD_PATH.fullmatch(key) and "," not in key:
                found[".".join((*within, key))] = None
                walk(field, (*within, key), seen)

    walk(schema, (), frozenset())
    return tuple(found)


def asks_whole(fields: Fields) -> bool:
    """Whether the caller asked for every field: AIP-157's ``*``, which keeps
    every field whatever else the mask names."""

    return WHOLE_RECORD in fields


def selected(records: list[dict[str, Any]], fields: Fields, declared: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Each record cut to the field mask ``fields`` names, in its own order.

    A mask names what the records carry, or a path their kind ``declared``
    (``field_paths``), which a record that lacks it is answered without.
    """

    selection = _selection(fields, frozenset(declared))
    return _cut(selection, records, selection.mask, ())


@dataclass(frozen=True, slots=True)
class _Selection:
    mask: Mask
    # The first field that names each path, so a refusal points at it.
    origin: dict[tuple[str, ...], int]
    declared: frozenset[str]

    def refuse(self, path: tuple[str, ...], error_type: str, msg: str, *, at: tuple[str, ...] = ()) -> NoReturn:
        loc = ["fields", self.origin[path], *at]
        raise FieldMaskRefusal([{"type": error_type, "loc": loc, "msg": msg}])


def _selection(fields: Fields, declared: frozenset[str]) -> _Selection:
    """The paths as one mask: shared prefixes merge, and a whole value wins."""

    if not fields:
        raise FieldMaskRefusal([{"type": "too_short", "loc": ["fields"], "msg": "List should have at least 1 item"}])
    if malformed := [index for index, field in enumerate(fields) if not _FIELD_PATH.fullmatch(field)]:
        raise FieldMaskRefusal(
            [
                {"type": "string_pattern_mismatch", "loc": ["fields", index], "msg": "String should match 'name.name'"}
                for index in malformed
            ]
        )
    mask: Mask = {}
    origin: dict[tuple[str, ...], int] = {}
    for index, field in enumerate(fields):
        segments = field.split(".")
        for depth in range(len(segments)):
            origin.setdefault(tuple(segments[: depth + 1]), index)
        _merge(mask, segments)
    return _Selection(mask, origin, declared)


def _merge(mask: Mask, segments: list[str]) -> None:
    *parents, leaf = segments
    node = mask
    for segment in parents:
        if segment in node and node[segment] is None:
            return  # The whole value is already kept.
        child = node.get(segment)
        if child is None:
            child = node[segment] = {}
        node = child
    node[leaf] = None


def _cut(selection: _Selection, records: list[dict[str, Any]], mask: Mask, at: tuple[str, ...]) -> list[dict[str, Any]]:
    """``records`` cut to ``mask``, every record at this depth checked together.

    A name no record here carries is refused with the names there are, so a
    typo is never answered with records that silently lack it. With no records
    the names are the ones their kind declares here; with neither there is
    nothing to name a key against.
    """

    known = list(dict.fromkeys(key for record in records for key in record))
    if not records:
        below = [path.split(".") for path in selection.declared]
        known = sorted({path[len(at)] for path in below if len(path) > len(at) and tuple(path[: len(at)]) == at})
    unknown = [key for key in mask if key not in known and ".".join((*at, key)) not in selection.declared]
    if (records or known) and unknown:
        choices = ", ".join(f"'{key}'" for key in known)
        selection.refuse((*at, unknown[0]), "literal_error", f"Input should be {choices}")
    cut = [{key: value for key, value in record.items() if key in mask and mask[key] is None} for record in records]
    for key, inner in mask.items():
        if inner is not None:
            _cut_within(selection, records, cut, key, inner, (*at, key))
    return [{key: out[key] for key in record if key in out} for record, out in zip(records, cut, strict=True)]


def _cut_within(
    selection: _Selection,
    records: list[dict[str, Any]],
    cut: list[dict[str, Any]],
    key: str,
    inner: Mask,
    path: tuple[str, ...],
) -> None:
    """Cut the objects under ``key``, one object or each element of a list."""

    elements: list[dict[str, Any]] = []
    for record in records:
        value = record.get(key)
        if isinstance(value, dict):
            elements.append(value)
        elif isinstance(value, list | tuple) and all(isinstance(element, dict) for element in value):
            elements.extend(value)
        elif key in record and value is not None:
            selection.refuse(path, "dict_type", "Input should be a valid dictionary", at=path)
    # The cut elements, put back where each came from.
    shaped = iter(_cut(selection, elements, inner, path))
    for record, out in zip(records, cut, strict=True):
        value = record.get(key)
        if isinstance(value, dict):
            out[key] = next(shaped)
        elif isinstance(value, list | tuple):
            out[key] = [next(shaped) for _ in value]
        elif key in record:
            out[key] = None
