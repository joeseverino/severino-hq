from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "object_type", "object_repr")
    list_filter = ("action", "object_type")
    search_fields = ("object_repr", "message", "user__username")
    readonly_fields = (
        "user",
        "action",
        "object_type",
        "object_id",
        "object_repr",
        "message",
        "metadata",
        "created_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
