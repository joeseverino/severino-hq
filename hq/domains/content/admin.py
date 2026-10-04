from django.contrib import admin

from .models import ContentItem


@admin.register(ContentItem)
class ContentItemAdmin(admin.ModelAdmin):
    list_display = ("title", "content_type", "status", "published_at", "updated_at")
    list_filter = ("status", "content_type")
    search_fields = ("title", "slug", "topic", "tags", "notes")
    prepopulated_fields = {"slug": ("title",)}
    filter_horizontal = (
        "related_projects",
        "related_assets",
        "related_expenses",
        "related_documentation",
    )
    readonly_fields = ("created_at", "updated_at")
