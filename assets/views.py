from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.urls import reverse, reverse_lazy
from django.utils.html import format_html
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from application.assets import asset_command_from_cleaned_data, save_asset
from application.deletion import delete_asset
from application.pages import PageAction, PageMixin, record_trail
from application.tables import TableColumn, TableFilter, TableListMixin, TableToggle
from application.writes import (
    ServiceCreateMixin,
    ServiceDeleteMixin,
    ServiceUpdateMixin,
)
from .forms import AssetForm
from .models import ASSET_CATEGORY_CHOICES, Asset


class AssetListView(PageMixin, TableListMixin, LoginRequiredMixin, ListView):
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
        TableColumn("Cost", "total_cost", "Lowest cost", "Highest cost"),
        TableColumn("% biz", "business_use_percentage", "Lowest business use", "Highest business use"),
        TableColumn("Est. deduct.", "estimated_deductible_amount", "Lowest deductible", "Highest deductible"),
        TableColumn("Status", "status"),
    )
    table_toggles = (TableToggle("missing_purchase", "Missing purchase info"),)
    table_default_sort = "-purchase_date"
    table_search_placeholder = "Search assets, vendors, serials, and notes…"

    def get_page_actions(self):
        return (PageAction("New asset", reverse("assets:create"), primary=True),)

    def get_queryset(self):
        qs = Asset.objects.all()
        if self.request.GET.get("missing_purchase"):
            qs = qs.filter(status=Asset.Status.ACTIVE).filter(
                Q(purchase_date__isnull=True) | Q(total_cost=0)
            )
        return self.apply_table_query(qs)


class AssetPage(PageMixin):
    """A page about one asset, or a new one: its trail runs back to the list."""

    def get_page_trail(self):
        return record_trail(
            ("Assets", reverse("assets:list")),
            getattr(self, "object", None),
            lambda asset: asset.item_name,
        )


class AssetDetailView(PageMixin, LoginRequiredMixin, DetailView):
    model = Asset
    template_name = "assets/asset_detail.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    context_object_name = "asset"
    queryset = Asset.objects.prefetch_related(
        "related_projects",
        "content_items",
        "documentation_records",
        "expenses",
        "receipts",
    )

    def get_page_title(self):
        return self.object.item_name

    def get_page_trail(self):
        return (("Assets", reverse("assets:list")),)

    def get_page_lede(self):
        return format_html(
            '{} · <span class="pill pill-{}">{}</span>',
            self.object.get_category_display(),
            self.object.status,
            self.object.get_status_display(),
        )

    def get_page_actions(self):
        slug = self.object.slug
        return (
            PageAction("Edit", reverse("assets:edit", args=[slug])),
            PageAction("Delete", reverse("assets:delete", args=[slug]), danger=True),
        )


class AssetWrite:
    """What every asset write shares, whichever direction it goes."""

    model = Asset
    noun = "Asset"
    result_key = "asset"
    identity_attr = "slug"
    identity_kwarg = "current_slug"


class AssetCreateView(AssetWrite, AssetPage, ServiceCreateMixin, LoginRequiredMixin, CreateView):
    page_title = "New asset"
    form_class = AssetForm
    template_name = "assets/asset_form.html"
    service = staticmethod(save_asset)
    command_from_cleaned_data = staticmethod(asset_command_from_cleaned_data)


class AssetUpdateView(AssetWrite, AssetPage, ServiceUpdateMixin, LoginRequiredMixin, UpdateView):
    page_title = "Edit asset"
    form_class = AssetForm
    template_name = "assets/asset_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    service = staticmethod(save_asset)
    command_from_cleaned_data = staticmethod(asset_command_from_cleaned_data)


class AssetDeleteView(AssetWrite, AssetPage, ServiceDeleteMixin, LoginRequiredMixin, DeleteView):
    page_title = "Delete asset?"
    template_name = "assets/asset_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("assets:list")
    context_object_name = "asset"
    service = staticmethod(delete_asset)
