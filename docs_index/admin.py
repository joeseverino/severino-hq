from django.contrib import admin

from .models import DocumentationRecord


@admin.register(DocumentationRecord)
class DocumentationRecordAdmin(admin.ModelAdmin):
    list_display = (
        "doc_id",
        "title",
        "doc_type",
        "environment",
        "status",
        "sensitivity",
        "last_reviewed",
    )
    list_filter = ("doc_type", "environment", "status", "sensitivity")
    search_fields = (
        "doc_id",
        "title",
        "system_service",
        "obsidian_path",
        "github_path",
        "notes",
    )
    filter_horizontal = ("related_projects", "related_assets", "related_expenses")
    readonly_fields = ("created_at", "updated_at")
