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
    # Presentation contract shared by every search surface (web, CLI, MCP):
    # how a hit in this scope is labeled without leaking str() internals
    # like "doc_id — title" into result lists.
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
        if self.title_field:
            return str(getattr(instance, self.title_field))
        return str(instance)

    def badge(self, instance) -> str:
        if self.badge_field:
            return str(getattr(instance, self.badge_field))
        return ""

    def timestamp(self, instance):
        return getattr(instance, self.timestamp_field, None)


DEFINITIONS = (
    SearchDefinition(
        "projects", Project, "slug",
        ("name", "slug", "description", "technologies_used", "notes"),
        label="Projects", title_field="name",
    ),
    SearchDefinition(
        "assets", Asset, "slug",
        ("item_name", "slug", "vendor", "serial_number", "category", "notes"),
        label="Assets", title_field="item_name",
    ),
    SearchDefinition(
        "content", ContentItem, "slug",
        ("title", "slug", "topic", "tags", "notes"),
        label="Content", title_field="title",
    ),
    SearchDefinition(
        "documentation", DocumentationRecord, "doc_id",
        ("doc_id", "title", "system_service", "obsidian_path", "github_path", "notes"),
        label="Docs", title_field="title", badge_field="doc_id",
    ),
    SearchDefinition(
        "expenses", Expense, "pk",
        ("vendor", "item", "category", "business_purpose", "notes"),
        label="Expenses",
    ),
    SearchDefinition(
        "receipts", Receipt, "pk",
        ("original_filename", "vendor", "notes"),
        label="Receipts",
    ),
    SearchDefinition(
        "audit", AuditLog, "pk",
        ("action", "object_type", "object_id", "object_repr", "message"),
        label="Audit log", timestamp_field="created_at",
    ),
)

BY_MODEL = {definition.model: definition for definition in DEFINITIONS}
BY_SCOPE = {definition.scope: definition for definition in DEFINITIONS}
