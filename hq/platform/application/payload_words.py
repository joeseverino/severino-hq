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

from hq.platform.core.digests import canonical

from .display_words import Quoted
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


# How many records of a list are said by name before the rest are counted.
NAMED = 3


def _list_words(items: list[Any], schema: Mapping[str, Any] | None) -> str:
    """A list of plain values said as those values, a list of records each
    known by a name of its own (``entry_names``) as its first names and a count
    of the rest, any other counted by what its schema calls one item, when that
    is a single word."""

    if all(_scalar(item) for item in items):
        words = [value_words(item) for item in items]
        joined = ", ".join(words)
        # An author's words joined only with more of theirs stay theirs.
        return Quoted(joined) if words and all(isinstance(each, Quoted) for each in words) else joined
    names = entry_names(items)
    if items and all(names):
        said = ", ".join(names[:NAMED])
        return said if len(names) <= NAMED else f"{said} and {counted(len(names) - NAMED, 'more', 'more')}"
    noun = lower_first(_items_title(schema))
    return counted(len(items), noun if noun and " " not in noun else "entry")


def _entry_name(entry: Any) -> str:
    """What a record in a list is known by: its first field that holds text
    (a grant's sources, a location's path, a doc's id), or nothing."""

    entry = document(entry)
    if not isinstance(entry, Mapping):
        return ""
    for value in entry.values():
        if isinstance(value, str) and value.strip():
            return value_words(value)
        if isinstance(value, (list, tuple)) and value and all(isinstance(item, str) for item in value):
            return value_words(value)
    return ""


def entry_names(items: list[Any]) -> list[str]:
    """Each entry's name (``_entry_name``) where no other entry of the list
    shares it, so a name points at one entry; "" where it has none."""

    names = [_entry_name(item) for item in items]
    shared = {name for name in names if names.count(name) > 1}
    return [name if name not in shared else "" for name in names]


def _plain_words(value: Any) -> str:
    """A value that is neither a record nor a list: a moment as HQ says one,
    text and numbers as they are, and nothing a page would refuse to print."""

    if isinstance(value, Quoted):
        return value
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


@dataclass(frozen=True, slots=True)
class Entry:
    """A step into a list of records: the entry's position, and the name it is
    known by in its list, when it has one of its own (``entry_names``)."""

    index: int
    name: str = ""

    def __str__(self) -> str:
        return self.name or f"Entry {self.index + 1}"


type Path = tuple[str | Entry, ...]


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


def _leaves(value: Any, path: Path) -> dict[Path, Any]:
    """Each value a document holds, by where it is: a record is opened by key,
    a list of records by position. A list of plain values is one value, said
    as its words.
    """

    value = document(value)
    if isinstance(value, Mapping) and value:
        found: dict[Path, Any] = {}
        for key, child in value.items():
            found.update(_leaves(child, (*path, str(key))))
        return found
    if isinstance(value, (list, tuple)) and not all(_scalar(item) for item in value):
        found = {}
        for index, (child, name) in enumerate(zip(value, entry_names(list(value)), strict=True)):
            found.update(_leaves(child, (*path, Entry(index, name))))
        return found
    return {path: value}


def path_words(path: Path) -> str:
    """Where a value is in a document, as a person names it: each key as a
    field label, each entry of a list by its name, else its position
    ("Grants › group:admins › Dst", "Grants › Entry 1 › Src").

    A key that is not a name (a hostname, a tag) is said as it is written.
    """

    return STEP.join(
        str(step) if isinstance(step, Entry) else field_label(step) if step.isidentifier() else step for step in path
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


# A row as it is found, beside its place: its path with each entry by its
# position alone, so an entry removed where another is added shares it.
type Found = list[tuple[tuple[str | int, ...], ChangeRow]]


def _differences(before: Any, after: Any, path: Path) -> Found:
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


def _leaf_rows(before: Any, after: Any, path: Path) -> Found:
    """Each value two documents hold at the same place that differs, where
    their shapes give nothing to match: a record or list on one side only is
    opened by key and position (``_leaves``)."""

    was_all = {} if before is _ABSENT else _leaves(before, path)
    now_all = {} if after is _ABSENT else _leaves(after, path)
    places = {_place(at): at for at in [*was_all, *now_all]}
    was_at = {_place(at): value for at, value in was_all.items()}
    now_at = {_place(at): value for at, value in now_all.items()}
    return [
        found
        for place, at in places.items()
        if (found := _row(at, was_at.get(place, _ABSENT), now_at.get(place, _ABSENT))) is not None
    ]


def _place(path: Path) -> tuple[str | int, ...]:
    return tuple(step.index if isinstance(step, Entry) else step for step in path)


def _row(path: Path, was: Any, now: Any) -> tuple[tuple[str | int, ...], ChangeRow] | None:
    was_words = "" if was is _ABSENT else value_words(was)
    now_words = "" if now is _ABSENT else value_words(now)
    if was_words == now_words:
        return None
    return _place(path), ChangeRow(
        label=path_words(path),
        before=was_words,
        after=now_words,
        change="added" if was is _ABSENT else "removed" if now is _ABSENT else "changed",
    )


def _canonical(value: Any) -> str:
    """An entry as text that two equal entries share, to match it by."""

    try:
        return canonical(document(value))
    except TypeError:
        return repr(value)


# How alike two entries' texts must be to be read as one entry changed rather
# than one removed and another added, and how far apart in their lists they may
# stand: ``difflib.Differ``'s own cutoff and window for pairing similar lines.
_ALIKE = 0.75
_WINDOW = 10


def _entries(before: list[Any], after: list[Any], path: Path) -> Found:
    """Two lists of records compared as ``git diff`` compares lines: the
    longest runs of equal entries are matched (``difflib.SequenceMatcher``,
    without its heuristic that treats a common entry of a long list as junk),
    and in what lies between, an entry alike enough to one on the other side
    is read as that entry changed, field by field, at the place it now stands,
    by the name it was known by, so the field that names it may move under
    it. Every other entry comes or goes whole, as one row."""

    was, now = [_canonical(entry) for entry in before], [_canonical(entry) for entry in after]
    was_names, now_names = entry_names(before), entry_names(after)
    rows: Found = []

    def whole(index: int, entry: Any, *, added: bool) -> None:
        step = Entry(index, (now_names if added else was_names)[index])
        found = _row((*path, step), _ABSENT if added else entry, entry if added else _ABSENT)
        if found is not None:
            rows.append(found)

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
            rows.extend(_differences(before[best], after[j], (*path, Entry(j, was_names[best] or now_names[j]))))
            done_a, done_b = best + 1, j + 1
        unpaired(done_a, ahi, done_b, bhi)
    return rows


def _coalesced(rows: Found) -> tuple[ChangeRow, ...]:
    """One row per place: an entry removed where another is added, at the same
    position, reads as that position changing from one to the other, named as
    the entry that now stands there."""

    found: dict[tuple[str | int, ...], ChangeRow] = {}
    for place, row in rows:
        was = found.get(place)
        if was is None:
            found[place] = row
        else:
            gone, came = (was, row) if was.change == "removed" else (row, was)
            found[place] = replace(gone, label=came.label, after=came.after, change="changed")
    return tuple(found.values())
