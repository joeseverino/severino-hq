"""A command's input, or a value stored beside a record, in a person's words.

A payload is a machine contract: lists of records, flags, nested documents.
Printed as Python prints it, a held request reads ``manifest to [{'doc_id': …``.
Read through the command's own schema, the same request reads "Manifest to 4
entries, confirm prune to yes".
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from difflib import SequenceMatcher
from typing import Any

from .labels import field_label, lower_first
from .moments import moment
from .printable import admitted
from .ui import counted
lazy from .moments import when

# How many plain facts a record may carry and still be said whole. Past this it
# is counted, because a sentence of twenty facts is not read.
SAID_WHOLE = 6
# The extended form a stamp is written in: a date with its dashes, then
# optionally a time and an offset. The basic form ("20261023") is also a
# number, so it is left to read as one.
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?")


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _plain(value: Any) -> bool:
    """A value said in a few words: a scalar, or a list of them."""

    return _scalar(value) or (isinstance(value, (list, tuple, set, frozenset)) and all(_scalar(item) for item in value))


def _items_title(schema: Mapping[str, Any] | None) -> str:
    """What one element of a list field is called, when its schema names it."""

    if not schema:
        return ""
    for option in (schema, *schema.get("anyOf", ())):
        title = (option.get("items") or {}).get("title", "")
        if title:
            return title
    return ""


def value_words(value: Any, schema: Mapping[str, Any] | None = None) -> str:
    """One value as a person reads it: a flag as yes or no, a list of words as
    those words, a list of records as a count, a small record as its facts.

    ``schema`` is the field's JSON Schema, when there is one; it names what a
    list holds.
    """

    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Mapping):
        return _record_words(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _list_words(list(value), schema)
    return _plain_words(value)


def _record_words(record: Mapping[Any, Any]) -> str:
    """A small record of plain facts (a list of words among them) said whole,
    any other counted by its fields."""

    if len(record) > SAID_WHOLE or not all(_plain(child) for child in record.values()):
        return counted(len(record), "field")
    return ", ".join(
        f"{lower_first(field_label(str(key)))} {value_words(child) or 'empty'}" for key, child in record.items()
    )


def _list_words(items: list[Any], schema: Mapping[str, Any] | None) -> str:
    """A list of plain values said as those values, any other counted by what
    its schema calls one item, when that is a single word."""

    if all(_scalar(item) for item in items):
        return ", ".join(value_words(item) for item in items)
    noun = lower_first(_items_title(schema))
    return counted(len(items), noun if noun and " " not in noun else "entry")


def _plain_words(value: Any) -> str:
    """A value that is neither a record nor a list: a moment as HQ says one,
    text and numbers as they are, and nothing a page would refuse to print."""

    if isinstance(value, str):
        value = _instant(value) or value
    if isinstance(value, date):
        return when(value)
    return str(admitted(value, "value_words"))


def _instant(text: str) -> date | None:
    """The day or instant ``text`` wholly names, if it names one."""

    text = text.strip()
    found = moment(text) if _STAMP.fullmatch(text) else None
    return found.date() if found and len(text) == len("YYYY-MM-DD") else found


def field_words(name: str, schema: Mapping[str, Any] | None = None) -> str:
    """A field's name as a person reads it, from its schema's title."""

    return field_label(name, (schema or {}).get("title"))


@dataclass(frozen=True, slots=True)
class ChangeRow:
    """One value that moves, named and valued in words.

    ``change`` is "added" when there was nothing before, "removed" when there
    is nothing after, and "changed" otherwise. A ``redacted`` value is said only to have changed.
    """

    label: str
    before: str
    after: str
    change: str
    redacted: bool = False


# Between the names of a value inside a document, outermost first.
STEP = " › "

_ABSENT = sentinel("_ABSENT")


def document(value: Any) -> Any:
    """A string that is really a JSON document, read as one.

    A field that carries a policy is a string as far as its schema is
    concerned, and compared as a string its change is two walls of text.
    """

    if isinstance(value, str) and value.strip()[:1] in {"{", "["}:
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _leaves(value: Any, path: tuple[str | int, ...]) -> dict[tuple[str | int, ...], Any]:
    """Each value a document holds, by where it is: a record is opened by key,
    a list of records by position. A list of plain values is one value, said
    as its words.
    """

    value = document(value)
    if isinstance(value, Mapping) and value:
        found: dict[tuple[str | int, ...], Any] = {}
        for key, child in value.items():
            found.update(_leaves(child, (*path, str(key))))
        return found
    if isinstance(value, (list, tuple)) and not all(_scalar(item) for item in value):
        found = {}
        for index, child in enumerate(value):
            found.update(_leaves(child, (*path, index)))
        return found
    return {path: value}


def path_words(path: tuple[str | int, ...]) -> str:
    """Where a value is in a document, as a person names it: each key as a
    field label, each list position as its entry ("Grants › Entry 1 › Src").

    A key that is not a name (a hostname, a tag) is said as it is written.
    """

    return STEP.join(
        f"Entry {step + 1}" if isinstance(step, int) else field_label(step) if step.isidentifier() else step
        for step in path
    )


def changed_words(before: Any, after: Any, *, within: tuple[str, ...] = ()) -> tuple[ChangeRow, ...]:
    """The values that differ between two documents, each named by where it is.

    Absent and empty are the same nothing: a validated record carries an unset
    field as a blank where a request omits it, and that is no change. The
    entries of a list of records are matched by content (``_entries``), so one
    inserted reads as one entry added, never as every later entry shifted.
    """

    return _coalesced(_differences(before, after, within))


def _record_list(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and not all(_scalar(item) for item in value)


def _differences(before: Any, after: Any, path: tuple[str | int, ...]) -> list[ChangeRow]:
    before, after = document(before), document(after)
    if isinstance(before, Mapping) and before and isinstance(after, Mapping) and after:
        # In the document's own order, then what only the new one holds.
        keys = [*before, *(key for key in after if key not in before)]
        return [
            row
            for key in keys
            for row in _differences(before.get(key, _ABSENT), after.get(key, _ABSENT), (*path, str(key)))
        ]
    if _record_list(before) and _record_list(after):
        return _entries(list(before), list(after), path)
    return _leaf_rows(before, after, path)


def _leaf_rows(before: Any, after: Any, path: tuple[str | int, ...]) -> list[ChangeRow]:
    """Each value two documents hold at the same place that differs, where
    their shapes give nothing to match: a record or list on one side only is
    opened by key and position (``_leaves``)."""

    was_all = {} if before is _ABSENT else _leaves(before, path)
    now_all = {} if after is _ABSENT else _leaves(after, path)
    return [
        row
        for at in [*was_all, *(at for at in now_all if at not in was_all)]
        if (row := _row(at, was_all.get(at, _ABSENT), now_all.get(at, _ABSENT))) is not None
    ]


def _row(path: tuple[str | int, ...], was: Any, now: Any) -> ChangeRow | None:
    was_words = "" if was is _ABSENT else value_words(was)
    now_words = "" if now is _ABSENT else value_words(now)
    if was_words == now_words:
        return None
    return ChangeRow(
        label=path_words(path),
        before=was_words,
        after=now_words,
        change="added" if was is _ABSENT else "removed" if now is _ABSENT else "changed",
    )


def _canonical(value: Any) -> str:
    """An entry as text that two equal entries share, to match it by."""

    try:
        return json.dumps(document(value), sort_keys=True, default=str)
    except TypeError:
        return repr(value)


# How alike two entries' texts must be to be read as one entry changed rather
# than one removed and another added, and how far apart in their lists they may
# stand: ``difflib.Differ``'s own cutoff and window for pairing similar lines.
_ALIKE = 0.75
_WINDOW = 10


def _entries(before: list[Any], after: list[Any], path: tuple[str | int, ...]) -> list[ChangeRow]:
    """Two lists of records compared as ``git diff`` compares lines: the
    longest runs of equal entries are matched (``difflib.SequenceMatcher``,
    without its heuristic that treats a common entry of a long list as junk),
    and in what lies between, an entry alike enough to one on the other side
    is read as that entry changed, field by field, and named where it now
    stands. Every other entry comes or goes whole, as one row."""

    was, now = [_canonical(entry) for entry in before], [_canonical(entry) for entry in after]
    rows: list[ChangeRow] = []

    def whole(index: int, entry: Any, *, added: bool) -> None:
        row = _row((*path, index), _ABSENT if added else entry, entry if added else _ABSENT)
        if row is not None:
            rows.append(row)

    def unpaired(alo: int, ahi: int, blo: int, bhi: int) -> None:
        for index in range(alo, ahi):
            whole(index, before[index], added=False)
        for index in range(blo, bhi):
            whole(index, after[index], added=True)

    for tag, alo, ahi, blo, bhi in SequenceMatcher(None, was, now, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        # As ``difflib.Differ._fancy_replace`` pairs lines: each new entry in
        # turn takes the most alike old one near it, past the last pair.
        alike = SequenceMatcher(autojunk=False)
        done_a, done_b = alo, blo
        for j in range(blo, bhi):
            alike.set_seq2(now[j])
            best, best_ratio = None, _ALIKE
            start = alo + (j - blo)
            for i in range(max(start - _WINDOW, done_a), min(start + _WINDOW + 1, ahi)):
                alike.set_seq1(was[i])
                if alike.real_quick_ratio() <= best_ratio or alike.quick_ratio() <= best_ratio:
                    continue
                if (ratio := alike.ratio()) > best_ratio:
                    best, best_ratio = i, ratio
            if best is None:
                continue
            unpaired(done_a, best, done_b, j)
            rows.extend(_differences(before[best], after[j], (*path, j)))
            done_a, done_b = best + 1, j + 1
        unpaired(done_a, ahi, done_b, bhi)
    return rows


def _coalesced(rows: list[ChangeRow]) -> tuple[ChangeRow, ...]:
    """One row per place: an entry removed where another is added, at the same
    position, reads as that position changing from one to the other."""

    found: dict[str, ChangeRow] = {}
    for row in rows:
        was = found.get(row.label)
        if was is None:
            found[row.label] = row
        else:
            gone, came = (was, row) if was.change == "removed" else (row, was)
            found[row.label] = replace(gone, after=came.after, change="changed")
    return tuple(found.values())
