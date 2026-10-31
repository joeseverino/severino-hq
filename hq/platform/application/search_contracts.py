"""Transport-neutral contracts for records projected into global search."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
lazy from datetime import datetime

lazy from django.db.models import Model


def search_lines(value: Any) -> Iterator[str]:
    """One readable line per leaf of a field value, whatever shape it has.

    A body is read by the index and by a person: the snippet under a result
    is cut straight out of it. ``str()`` on a ``JSONField`` serves neither: it
    renders Python, and the punctuation carrying the meaning is what the
    tokenizer discards, so a key and its value would index as unrelated words.
    """

    # Keys stay verbatim; FTS5 already splits ``key_expiry_disabled`` on the
    # underscores.

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
        # Collapsed, so a field holding a policy document does not spend the
        # snippet window on escaped newlines.
        text = " ".join(str(value).split())
        if text:
            yield text


@dataclass(frozen=True, slots=True)
class SearchDefinition:
    scope: str
    model: type
    identifier_field: str
    fields: tuple[str, ...]
    label: str = ""
    title_field: str = ""
    badge_field: str = ""
    timestamp_field: str = "updated_at"
    # A property holding readable text for the result; blank uses the indexed body.
    snippet_field: str = ""

    def object_id(self, instance: Model) -> str:
        return str(getattr(instance, self.identifier_field))

    # The names are a plugin's declaration, never checked against its model,
    # and the default timestamp names a field a model may not have.
    def body(self, instance: Model) -> str:
        return "\n".join(line for field in self.fields for line in search_lines(getattr(instance, field, None)))

    def title(self, instance: Model) -> str:
        return str(getattr(instance, self.title_field)) if self.title_field else str(instance)

    def badge(self, instance: Model) -> str:
        return str(getattr(instance, self.badge_field)) if self.badge_field else ""

    def timestamp(self, instance: Model) -> datetime | None:
        return getattr(instance, self.timestamp_field, None)

    def snippet_text(self, instance: Model) -> str:
        """What a result shows under its title, or "" to cut it from the body."""

        if not self.snippet_field:
            return ""
        return " ".join(str(getattr(instance, self.snippet_field, "") or "").split())

    def url(self, instance: Model) -> str:
        # Django leaves get_absolute_url for a model to define or not.
        return instance.get_absolute_url() if hasattr(instance, "get_absolute_url") else ""
