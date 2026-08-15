"""Transport-neutral contracts for records projected into global search."""

from __future__ import annotations

from dataclasses import dataclass


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
            str(value)
            for field in self.fields
            if (value := getattr(instance, field, "")) not in (None, "")
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
