"""What an audit row says in words: its message from the fields it changed,
and its title from the operation it was written in.

A row stores what moved as data (``metadata.changes``) and its operation by
name (``metadata.operation``). The same words are derived from those when the
row is written and when an older row is read, so a row written before rows
said anything reads the same way.
"""

from dataclasses import dataclass
from typing import Any

from django.utils.text import Truncator

from hq.platform.application.payload_words import STEP, ChangeRow, path_words, value_words
from hq.platform.application.ui import counted
lazy from hq.domains.control_plane.bridge_actions import ACTIONS
lazy from hq.platform.application import capabilities
lazy from hq.platform.application.approvals import LAPSE, REQUEST
lazy from hq.platform.application.outbound_work import job_names
lazy from hq.platform.application.reports import EXPORT
lazy from hq.platform.application.security import MCP_OPERATION

lazy from .models import AuditLog

# What a value whose words never reach the log is stored as.
REDACTED = "«redacted»"
# A message is one short line: past this many fields it counts the rest.
FIELDS_SAID = 3
VALUE_WORDS = 40
MESSAGE_CHARS = 160


def change_rows(changes: Any) -> tuple[ChangeRow, ...]:
    """Each field a row changed, named and valued in words.

    A plain field is stored as its pair of values; a record or list as the
    values in it that moved, each already in words, by where it is. A field
    whose values never reach the log says only that it changed, as blank
    ``before`` and ``after`` with ``redacted`` set.
    """

    if not isinstance(changes, dict):
        return ()
    rows: list[ChangeRow] = []
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
    return tuple(rows)


def _row(field: str, pair: list[Any]) -> ChangeRow:
    before, after = (str(value) for value in pair)
    if REDACTED in (before, after):
        return ChangeRow(label=field, before="", after="", change="changed", redacted=True)
    change = "added" if not before else "removed" if not after else "changed"
    return ChangeRow(label=field, before=before, after=after, change=change)


def changes_said(changes: Any) -> str:
    """What a row's ``changes`` moved, in one short line: "State: queued to claimed"."""

    rows = change_rows(changes)
    said = [_said(row) for row in rows[:FIELDS_SAID]]
    if len(rows) > FIELDS_SAID:
        said.append(counted(len(rows) - FIELDS_SAID, "more field", "more fields"))
    return Truncator("; ".join(said)).chars(MESSAGE_CHARS)


def _said(row: ChangeRow) -> str:
    field = row.label
    if row.redacted or not (row.before or row.after):
        return f"{field} changed"
    before, after = Truncator(row.before).chars(VALUE_WORDS), Truncator(row.after).chars(VALUE_WORDS)
    if not before:
        return f"{field}: {after}"
    if not after:
        return f"{field}: cleared, was {before}"
    return f"{field}: {before} to {after}"


def _titled() -> dict[str, str]:
    """The operations HQ binds that no capability, job or controller action
    declares, each titled where it is bound."""

    return {operation: operation.title for operation in (REQUEST, LAPSE, MCP_OPERATION, EXPORT)}


# A controller's call runs as ``bridge.<action>``, and one the bridge turned
# away as ``bridge.<action>.refused``, as the bridge binds them.
BRIDGE = "bridge."
REFUSED = ".refused"
REFUSED_TITLE = "HQ turned away a controller's call"


def title_of(operation: str) -> str | None:
    """What a person calls the operation named ``operation``, in the words of
    whatever declared it, or None when nothing did: a capability's title, a
    job's label, a controller action's title, or the title bound beside its
    name.

    The one place an operation's name becomes its title, so a name nothing
    declares is never shown as its code dressed up as words.
    """

    if not operation:
        return None
    spec = capabilities.capability_spec(operation)
    if spec is not None:
        return spec.title
    if title := job_names().get(operation) or _titled().get(operation):
        return title
    if operation.startswith(BRIDGE):
        action = operation.removeprefix(BRIDGE)
        refused = action.endswith(REFUSED)
        name = action.removesuffix(REFUSED)
        for declared in ACTIONS:
            if declared.name == name:
                return REFUSED_TITLE if refused else declared.title
    return None


@dataclass(frozen=True, slots=True)
class RowWords:
    """What one stored row says: the title of its operation, blank when
    nothing declared one, and its message."""

    title: str
    message: str


def words_of(row: AuditLog) -> RowWords:
    """A stored row in words, whenever it was written.

    The title is read from the operation the row names, never from its stored
    text, which on older rows is a code or a state. The message is the one the
    row stored, else what its stored changes moved, so a row written before
    updates said anything reads as a new one does.
    """

    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    operation = metadata.get("operation")
    return RowWords(
        title=title_of(operation if isinstance(operation, str) else "") or "",
        message=row.message or changes_said(metadata.get("changes")),
    )
