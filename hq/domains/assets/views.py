from typing import override

from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    TemplateView,
    UpdateView,
)

from hq.platform.application import related
from hq.platform.application.assets import managed_domain
from hq.platform.application.documentation import related_documents
from hq.platform.application.entity_links import entity_link
from hq.platform.application.expenses import said_totals
from hq.platform.application.labels import dotted_html
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.moments import stamp_day
from hq.platform.application.pages import PageAction, PageMixin, RecordPageMixin
from hq.platform.application.references import reference_of, referenced_by_row
from hq.platform.application.security import web_principal
from hq.platform.application.tables import Cell, TableColumn
from hq.platform.application.ui import measured
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin
from hq.platform.core.templatetags.status_tags import status_pill

from .models import ASSET_CATEGORY_CHOICES, PAYMENT_METHOD_CHOICES, Asset

ASSETS_TRAIL = (("Assets", reverse_lazy("assets:list")),)
_CATEGORIES = dict(ASSET_CATEGORY_CHOICES)


class AssetListView(ListPageMixin, TemplateView):
    resource = "assets"
    table_empty = "No assets yet."
    template_name = "assets/asset_list.html"
    page_title = "Assets"
    table_selectable = True
    table_columns = (
        TableColumn(
            "Item",
            "item_name",
            cell=lambda row: Cell.of(entity_link("asset", row["slug"], label=row["item_name"]), name=True),
        ),
        TableColumn("Vendor", "vendor", cell=lambda row: Cell(row["vendor"])),
        TableColumn("Category", "category", cell=lambda row: Cell(_CATEGORIES[row["category"]])),
        TableColumn(
            "Purchased", "purchase_date", cell=lambda row: Cell(moment=stamp_day(row["purchase_date"]), muted=True)
        ),
        TableColumn("Cost", "total_cost", css="key-col", cell=lambda row: Cell(amount=row["total_cost"])),
        TableColumn(
            "Business use",
            "business_use_percentage",
            cell=lambda row: Cell(measured(row["business_use_percentage"], "%"), muted=True),
        ),
        TableColumn(
            "Deductible (est.)",
            "estimated_deductible_amount",
            cell=lambda row: Cell(amount=row["estimated_deductible_amount"]),
        ),
        TableColumn("Status", "status", cell=lambda row: Cell.state(Asset.Status(row["status"]))),
    )
    table_search_placeholder = "Search assets, vendors, serials, and notes…"

    @override
    def get_page_actions(self):
        return (PageAction.command("asset.create", primary=True),)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["totals"] = said_totals(self.collection["totals"], narrowed=bool(context["table"]["active_count"]))
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
