"""Capability answers that name a resource's record type, kept apart from their handlers.

A record type lives with its resource's spec, which loads the handler's own
module, so a handler names these lazily and they are read once every module
has loaded.
"""

from typing import Literal, TypedDict

from hq.domains.projects.specs import ProjectRecord
from hq.domains.receipts.specs import ReceiptRow

from .calendar_specs import EntryRecord
from .receipts import StoredReceiptFile
from .record_saves import Created


class DependencyUpdatesSet(TypedDict):
    """The project, with its dependency updates as the call left them."""

    ok: Literal[True]
    project: ProjectRecord


class EntrySaved(TypedDict):
    """The calendar entry as saved, and whether the call created it."""

    ok: Literal[True]
    created: Created
    entry: EntryRecord


class ReceiptSaved(TypedDict):
    """The receipt as saved, and whether the call created it."""

    ok: Literal[True]
    created: Created
    receipt: ReceiptRow


class ReceiptUploaded(ReceiptSaved):
    """The receipt as saved with its file, and whether the call created it."""

    file: StoredReceiptFile
