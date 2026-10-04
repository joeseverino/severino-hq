from django.contrib import admin

from .models import Receipt


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "uploaded_at",
        "vendor",
        "date",
        "amount",
        "related_expense",
        "related_asset",
    )
    list_filter = ("date",)
    search_fields = ("vendor", "notes", "original_filename")
    raw_id_fields = ("related_expense", "related_asset")
    readonly_fields = (
        "uploaded_at",
        "original_filename",
        "content_type",
        "size_bytes",
        "created_at",
        "updated_at",
    )
