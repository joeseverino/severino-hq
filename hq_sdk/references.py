"""References: how a record names a thing in HQ, and how a page lists what names it.

A model keeps a reference in a ``ReferenceField`` beside a ``<name>_name``
column, and says its own rows can be referred to with ``referable =
Referable(...)``. Both are found on the installed models, so nothing is
registered. A template shows one with ``{% reference row "field" as link %}``
and ``{% entity link %}``, and what names a row with ``{% referenced_by row %}``.

A sync that copies records in stores what ``as_stored`` answers, and a domain
reports its own rows whose reference names nothing with ``dangling``.
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
