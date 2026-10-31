"""References: how a record names a thing in HQ, and how a page lists what names it.

A model keeps one in a ``ReferenceField`` beside a ``<name>_name`` column, and
says its rows may be named with ``referable = Referable(...)``; both are found
on the installed models, so nothing is registered. The fields, tags and
helpers: docs/Plugins.md, References.
"""

from hq.platform.application.references import (
    CERTIFICATE,
    Mention,
    Mentions,
    Referable,
    ReferenceField,
    ReferencePickerMixin,
    as_stored,
    dangling,
    reference_of,
    referenced_by,
    referenced_by_row,
)

__all__ = [
    "CERTIFICATE",
    "Mention",
    "Mentions",
    "Referable",
    "ReferenceField",
    "ReferencePickerMixin",
    "as_stored",
    "dangling",
    "reference_of",
    "referenced_by",
    "referenced_by_row",
]
