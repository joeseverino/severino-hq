"""What an audit row says in words: its message from the fields it changed.

A row stores what moved as data (``metadata.changes``). The same words are
derived from it when the row is written, as its message, and when an older
row is read, so a row written before rows said anything reads the same way.
"""

from typing import Any

from hq.platform.application.payload_words import STEP, path_words, value_words

# What a value whose words never reach the log is stored as.
REDACTED = "«redacted»"
# A message is one short line: past this many fields it counts the rest.
FIELDS_SAID = 3
VALUE_WORDS = 40
MESSAGE_CHARS = 160


def change_rows(changes: Any) -> list[dict[str, str]]:
    """Each field a row changed, as ``field``, ``before`` and ``after`` in words.

    A plain field is stored as its pair of values; a record or list as the
    values in it that moved, each already in words, by where it is. A field
    whose values never reach the log says only that it changed, as blank
    ``before`` and ``after`` with ``redacted`` set.
    """

    if not isinstance(changes, dict):
        return []
    rows = []
    for field, change in changes.items():
        named = path_words((str(field),))
        if isinstance(change, dict):
            rows.extend(
                _row(f"{named}{STEP}{inner}" if inner else named, pair)
                for inner, pair in change.items()
                if isinstance(pair, list) and len(pair) == 2
            )
        elif isinstance(change, list) and len(change) == 2:
            rows.append(_row(named, [value_words(change[0]), value_words(change[1])]))
    return rows


def _row(field: str, pair: list[Any]) -> dict[str, str]:
    before, after = (str(value) for value in pair)
    if REDACTED in (before, after):
        return {"field": field, "before": "", "after": "", "redacted": "yes"}
    return {"field": field, "before": before, "after": after}


def changes_said(changes: Any) -> str:
    """What a row's ``changes`` moved, in one short line: "State: queued to claimed"."""

    rows = change_rows(changes)
    said = [_said(row) for row in rows[:FIELDS_SAID]]
    if len(rows) > FIELDS_SAID:
        rest = len(rows) - FIELDS_SAID
        said.append(f"{rest} more field{'s' if rest != 1 else ''}")
    return _cut("; ".join(said), MESSAGE_CHARS)


def _said(row: dict[str, str]) -> str:
    field = row["field"]
    if row.get("redacted") or not (row["before"] or row["after"]):
        return f"{field} changed"
    before, after = _cut(row["before"], VALUE_WORDS), _cut(row["after"], VALUE_WORDS)
    if not before:
        return f"{field}: {after}"
    if not after:
        return f"{field}: cleared, was {before}"
    return f"{field}: {before} to {after}"


def _cut(words: str, chars: int) -> str:
    return words if len(words) <= chars else f"{words[: chars - 1]}…"
