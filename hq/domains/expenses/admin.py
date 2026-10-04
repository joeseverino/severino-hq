from django.contrib import admin

from .models import Expense


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "vendor",
        "item",
        "category",
        "total_cost",
        "business_use_percentage",
        "estimated_deductible_amount",
    )
    list_filter = ("category", "payment_method")
    search_fields = ("vendor", "item", "business_purpose", "notes")
    date_hierarchy = "date"
    readonly_fields = ("estimated_deductible_amount", "created_at", "updated_at")
    autocomplete_fields = ()
    raw_id_fields = (
        "related_project",
        "related_asset",
        "related_content",
        "related_documentation",
    )
