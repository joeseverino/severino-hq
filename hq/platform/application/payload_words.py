"""A command's input, or a value stored beside a record, in a person's words.

A payload is a machine contract: lists of records, flags, nested documents.
Printed as Python prints it, a held request reads ``manifest to [{'doc_id': …``.
Read through the command's own schema, the same request reads "Manifest to 4
entries, confirm prune to yes".
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from hq.platform.core.print_gate import admitted, is_stamp

from .labels import field_label, lower_first
from .timestamps import moment
from .ui import counted
lazy from .moments import when

# How many plain facts a record may carry and still be said whole. Past this it
# is counted, because a sentence of twenty facts is not read.
SAID_WHOLE = 6


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


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
        if len(value) <= SAID_WHOLE and all(_scalar(child) for child in value.values()):
            return ", ".join(
                f"{lower_first(field_label(str(key)))} {value_words(child) or 'empty'}" for key, child in value.items()
            )
        return counted(len(value), "field")
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if all(_scalar(item) for item in items):
            return ", ".join(value_words(item) for item in items)
        noun = lower_first(_items_title(schema))
        return counted(len(items), noun if noun and " " not in noun else "entry")
    return _plain_words(value)


def _plain_words(value: Any) -> str:
    """A value that is neither a record nor a list: a moment as HQ says one,
    text and numbers as they are, and nothing a page would refuse to print."""

    if isinstance(value, str) and is_stamp(value):
        text = value.strip()
        value = date.fromisoformat(text) if len(text) == len("YYYY-MM-DD") else moment(text)
    if isinstance(value, date):
        return when(value)
    return str(admitted(value))


def field_words(name: str, schema: Mapping[str, Any] | None = None) -> str:
    """A field's name as a person reads it, from its schema's title."""

    return field_label(name, (schema or {}).get("title"))


@dataclass(frozen=True, slots=True)
class ChangeRow:
    """One value that moves, named and valued in words.

    ``change`` is "added" when there was nothing before, "removed" when there
    is nothing after, and "changed" otherwise.
    """

    label: str
    before: str
    after: str
    change: str


# Between the names of a value inside a document, outermost first.
STEP = " › "

_ABSENT = object()


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
    field as a blank where a request omits it, and that is no change.
    """

    was_all = _leaves(before, within)
    now_all = _leaves(after, within)
    rows = []
    # In the document's own order, then what only the new one holds.
    for path in [*was_all, *(path for path in now_all if path not in was_all)]:
        was = was_all.get(path, _ABSENT)
        now = now_all.get(path, _ABSENT)
        was_words = "" if was is _ABSENT else value_words(was)
        now_words = "" if now is _ABSENT else value_words(now)
        if was_words == now_words:
            continue
        rows.append(
            ChangeRow(
                label=path_words(path),
                before=was_words,
                after=now_words,
                change="added" if was is _ABSENT else "removed" if now is _ABSENT else "changed",
            )
        )
    return tuple(rows)
