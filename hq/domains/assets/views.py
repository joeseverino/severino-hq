from typing import override

from django.db.models import Q
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    TemplateView,
    UpdateView,
)

from hq.platform.application import related
from hq.platform.application.assets import managed_domain
from hq.platform.application.documentation import related_documents
from hq.platform.application.expenses import cost_totals
from hq.platform.application.labels import dotted_html
from hq.platform.application.pages import PageAction, PageMixin, RecordPageMixin
from hq.platform.application.references import reference_of, referenced_by_row
from hq.platform.application.security import web_principal
from hq.platform.application.tables import TableColumn, TableFilter, TableListMixin, TableToggle
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin
from hq.platform.core.templatetags.status_tags import status_pill

from .models import ASSET_CATEGORY_CHOICES, PAYMENT_METHOD_CHOICES, Asset

ASSETS_TRAIL = (("Assets", reverse_lazy("assets:list")),)


class AssetListView(PageMixin, TableListMixin, ListView):
    table_empty = "No assets yet."
    model = Asset
    template_name = "assets/asset_list.html"
    paginate_by = 25
    page_title = "Assets"
    table_search_scope = "assets"
    table_selectable = True
    table_filters = (
        TableFilter("status", "Status", "status", Asset.Status.choices),
        TableFilter("category", "Category", "category", ASSET_CATEGORY_CHOICES),
    )
    table_columns = (
        TableColumn("Item", "item_name", "Name A–Z", "Name Z–A"),
        TableColumn("Vendor", "vendor", "Vendor A–Z", "Vendor Z–A"),
        TableColumn("Category", "category"),
        TableColumn("Purchased", "purchase_date", "Oldest purchase", "Newest purchase"),
        TableColumn("Cost", "total_cost", "Lowest cost", "Highest cost", css="key-col"),
        TableColumn("Business use", "business_use_percentage", "Lowest business use", "Highest business use"),
        TableColumn("Deductible (est.)", "estimated_deductible_amount", "Lowest deductible", "Highest deductible"),
        TableColumn("Status", "status"),
    )
    table_toggles = (TableToggle("missing_purchase", "No date or cost"),)
    table_default_sort = "-purchase_date"
    table_search_placeholder = "Search assets, vendors, serials, and notes…"

    @override
    def get_page_actions(self):
        return (PageAction.command("asset.create", primary=True),)

    @override
    def get_queryset(self):
        qs = Asset.objects.all()
        if self.request.GET.get("missing_purchase"):
            qs = qs.filter(status=Asset.Status.ACTIVE).filter(Q(purchase_date__isnull=True) | Q(total_cost=0))
        return self.apply_table_query(qs)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["totals"] = cost_totals(self.object_list, narrowed=bool(context["table"]["active_count"]))
        return context


class AssetPage(PageMixin):
    """A page about one asset, or a new one: its trail runs back to the list."""

    model = Asset
    page_trail = ASSETS_TRAIL

    @override
    def trail_label(self, record):
        return record.item_name


class AssetDetailView(RecordPageMixin, TemplateView):
    page_trail = ASSETS_TRAIL
    template_name = "assets/asset_detail.html"
    resource = "assets"
    context_object_name = "asset"
    model = Asset
    stored_related = ("related_projects", "content_items", "documentation_records", "expenses", "receipts")

    @override
    def get_page_title(self):
        return self.record["item_name"]

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        asset = self.stored
        documents = related_documents(asset.documentation_records.all(), listed=asset.related_projects.all())
        principal = web_principal(self.request.user)
        context["thing"] = reference_of(asset, "infrastructure", principal=principal)
        # A domain asset with no link is matched to the domain of its name.
        context["domain"] = None if asset.infrastructure else managed_domain(asset)
        context["payment_method"] = dict(PAYMENT_METHOD_CHOICES).get(self.record["payment_method"], "")
        context["related"] = related.related(
            related.projects(asset.related_projects.all(), documents.projects),
            related.writeups(asset.content_items.all()),
            related.expenses(asset.expenses.all()),
            related.receipts(asset.receipts.all()),
            *documents.groups,
            *referenced_by_row(asset, principal=principal),
        )
        return context

    @override
    def get_page_lede(self):
        return dotted_html(
            dict(ASSET_CATEGORY_CHOICES)[self.record["category"]],
            status_pill(Asset.Status(self.record["status"])),
        )

    @override
    def get_page_actions(self):
        slug = self.record["slug"]
        return (
            PageAction.command("asset.update", slug),
            PageAction.command("asset.delete", slug),
        )


# What saving the asset's form does, said under it.
FORM_PAGE = {"form_note": "Deductible estimate = cost × business use. Updated when you save."}


class AssetCreateView(AssetPage, RecordFormMixin, CreateView):
    page_title = "New asset"
    extra_context = FORM_PAGE


class AssetUpdateView(AssetPage, RecordFormMixin, UpdateView):
    page_title = "Edit asset"
    extra_context = FORM_PAGE


class AssetDeleteView(AssetPage, RecordDeleteMixin, DeleteView):
    page_title = "Delete asset?"
    template_name = "assets/asset_confirm_delete.html"
    success_url = reverse_lazy("assets:list")
    context_object_name = "asset"
