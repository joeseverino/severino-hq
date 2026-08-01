"""Search projection definitions for HQ record types."""

from __future__ import annotations

from dataclasses import dataclass

from assets.models import Asset
from content.models import ContentItem
from core.models import AuditLog
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt


@dataclass(frozen=True)
class SearchDefinition:
    scope: str
    model: type
    identifier_field: str
    fields: tuple[str, ...]

    def object_id(self, instance) -> str:
        return str(getattr(instance, self.identifier_field))

    def body(self, instance) -> str:
        return "\n".join(
            str(value)
            for field in self.fields
            if (value := getattr(instance, field, "")) not in (None, "")
        )


DEFINITIONS = (
    SearchDefinition(
        "projects", Project, "slug",
        ("name", "slug", "description", "technologies_used", "notes"),
    ),
    SearchDefinition(
        "assets", Asset, "slug",
        ("item_name", "slug", "vendor", "serial_number", "category", "notes"),
    ),
    SearchDefinition(
        "content", ContentItem, "slug",
        ("title", "slug", "topic", "tags", "notes"),
    ),
    SearchDefinition(
        "documentation", DocumentationRecord, "doc_id",
        ("doc_id", "title", "system_service", "obsidian_path", "github_path", "notes"),
    ),
    SearchDefinition(
        "expenses", Expense, "pk",
        ("vendor", "item", "category", "business_purpose", "notes"),
    ),
    SearchDefinition(
        "receipts", Receipt, "pk",
        ("original_filename", "vendor", "notes"),
    ),
    SearchDefinition(
        "audit", AuditLog, "pk",
        ("action", "object_type", "object_id", "object_repr", "message"),
    ),
)

BY_MODEL = {definition.model: definition for definition in DEFINITIONS}
BY_SCOPE = {definition.scope: definition for definition in DEFINITIONS}
