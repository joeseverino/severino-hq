from django.contrib import admin

from .models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "status", "updated_at")
    list_filter = ("status", "category")
    search_fields = ("name", "slug", "description", "technologies_used", "notes")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
