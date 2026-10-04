from django.contrib import admin

from .models import ManagedResource, OperationRequest


@admin.register(ManagedResource)
class ManagedResourceAdmin(admin.ModelAdmin):
    list_display = (
        "key",
        "kind",
        "enabled",
        "generation",
        "observed_generation",
        "last_observed_at",
    )
    list_filter = ("kind", "enabled")
    search_fields = ("key",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(OperationRequest)
class OperationRequestAdmin(admin.ModelAdmin):
    list_display = ("resource", "action", "state", "requested_actor", "created_at")
    list_filter = ("action", "state", "requested_interface")
    search_fields = ("resource__key", "requested_actor", "reason")
    readonly_fields = (
        "id",
        "resource",
        "action",
        "state",
        "requested_by",
        "requested_actor",
        "requested_interface",
        "reason",
        "idempotency_key",
        "input",
        "result",
        "claimed_by",
        "claimed_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
