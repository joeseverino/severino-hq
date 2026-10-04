from django.contrib import admin

from .models import Asset


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = (
        "item_name",
        "vendor",
        "category",
        "purchase_date",
        "total_cost",
        "business_use_percentage",
        "estimated_deductible_amount",
        "status",
    )
    list_filter = ("status", "category")
    search_fields = ("item_name", "vendor", "serial_number", "notes")
    prepopulated_fields = {"slug": ("item_name",)}
    filter_horizontal = ("related_projects",)
    readonly_fields = ("estimated_deductible_amount", "created_at", "updated_at")
