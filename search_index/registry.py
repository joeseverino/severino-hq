"""Search projection definitions for HQ record types."""

from __future__ import annotations

from assets.models import Asset
from application.search_contracts import SearchDefinition
from content.models import ContentItem
from core.models import AuditLog
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt
from application.plugins import plugin_search_definitions

CORE_DEFINITIONS = (
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

DEFINITIONS = (*CORE_DEFINITIONS, *plugin_search_definitions())

BY_MODEL = {definition.model: definition for definition in DEFINITIONS}
BY_SCOPE = {definition.scope: definition for definition in DEFINITIONS}
