"""Transport-neutral contracts for records projected into global search."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


def search_lines(value: Any) -> Iterator[str]:
    """One readable line per leaf of a field value, whatever shape it has.

    A body is read by the index and by a person -- the snippet under a result
    is cut straight out of it. ``str()`` on a ``JSONField`` served neither: it
    rendered Python (``{'key_expiry_disabled': False}``), and the punctuation
    carrying the meaning is the punctuation the tokenizer discards, so the key
    and its value were indexed as unrelated words.

    Keys stay verbatim; FTS5 already splits ``key_expiry_disabled`` on the
    underscores. Whitespace inside a value is collapsed, because a field
    holding a policy document spent the snippet window on escaped newlines.
    """

    if isinstance(value, dict):
        for key, item in value.items():
            for line in search_lines(item):
                yield f"{key}: {line}"
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from search_lines(item)
    elif isinstance(value, bool):
        # Ahead of the numeric branch, since `bool` is an `int`. Words rather
        # than `False`, because a snippet is read by a person and neither
        # spelling is more searchable than the other.
        yield "yes" if value else "no"
    elif value is None:
        return
    else:
        text = " ".join(str(value).split())
        if text:
            yield text


@dataclass(frozen=True)
class SearchDefinition:
    scope: str
    model: type
    identifier_field: str
    fields: tuple[str, ...]
    label: str = ""
    title_field: str = ""
    badge_field: str = ""
    timestamp_field: str = "updated_at"

    def object_id(self, instance) -> str:
        return str(getattr(instance, self.identifier_field))

    def body(self, instance) -> str:
        return "\n".join(
            line
            for field in self.fields
            for line in search_lines(getattr(instance, field, None))
        )

    def title(self, instance) -> str:
        return (
            str(getattr(instance, self.title_field))
            if self.title_field
            else str(instance)
        )

    def badge(self, instance) -> str:
        return str(getattr(instance, self.badge_field)) if self.badge_field else ""

    def timestamp(self, instance):
        return getattr(instance, self.timestamp_field, None)
