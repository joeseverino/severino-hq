from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from application.assets import asset_command_from_cleaned_data, save_asset
from application.deletion import delete_asset
from application.tables import TableFilter, TableListMixin, TableSort, TableToggle
from application.writes import (
    ServiceCreateMixin,
    ServiceDeleteMixin,
    ServiceUpdateMixin,
)
from .forms import AssetForm
from .models import ASSET_CATEGORY_CHOICES, Asset


class AssetListView(TableListMixin, LoginRequiredMixin, ListView):
    model = Asset
    template_name = "assets/asset_list.html"
    context_object_name = "assets_list"
    paginate_by = 25
    table_search_scope = "assets"
    table_filters = (
        TableFilter("status", "Status", "status", Asset.Status.choices),
        TableFilter("category", "Category", "category", ASSET_CATEGORY_CHOICES),
    )
    table_sorts = (
        TableSort("-purchase_date", "Newest purchase", "-purchase_date"),
        TableSort("purchase_date", "Oldest purchase", "purchase_date"),
        TableSort("item_name", "Name A–Z", "item_name"),
        TableSort("-item_name", "Name Z–A", "-item_name"),
        TableSort("vendor", "Vendor A–Z", "vendor"),
        TableSort("-vendor", "Vendor Z–A", "-vendor"),
        TableSort("-total_cost", "Highest cost", "-total_cost"),
        TableSort("total_cost", "Lowest cost", "total_cost"),
        TableSort("status", "Status", "status"),
        TableSort("-status", "Status reverse", "-status"),
        TableSort("category", "Category", "category"),
        TableSort("-category", "Category reverse", "-category"),
        TableSort(
            "business_use_percentage", "Lowest business use", "business_use_percentage"
        ),
        TableSort(
            "-business_use_percentage",
            "Highest business use",
            "-business_use_percentage",
        ),
        TableSort(
            "estimated_deductible_amount",
            "Lowest deductible",
            "estimated_deductible_amount",
        ),
        TableSort(
            "-estimated_deductible_amount",
            "Highest deductible",
            "-estimated_deductible_amount",
        ),
    )
    table_toggles = (TableToggle("missing_purchase", "Missing purchase info"),)
    table_default_sort = "-purchase_date"
    table_search_placeholder = "Search assets, vendors, serials, and notes…"

    def get_queryset(self):
        qs = Asset.objects.all()
        if self.request.GET.get("missing_purchase"):
            qs = qs.filter(status=Asset.Status.ACTIVE).filter(
                Q(purchase_date__isnull=True) | Q(total_cost=0)
            )
        return self.apply_table_query(qs)


class AssetDetailView(LoginRequiredMixin, DetailView):
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


class AssetWrite:
    """What every asset write shares, whichever direction it goes."""

    model = Asset
    noun = "Asset"
    result_key = "asset"
    identity_attr = "slug"
    identity_kwarg = "current_slug"


class AssetCreateView(AssetWrite, ServiceCreateMixin, LoginRequiredMixin, CreateView):
    form_class = AssetForm
    template_name = "assets/asset_form.html"
    service = staticmethod(save_asset)
    command_from_cleaned_data = staticmethod(asset_command_from_cleaned_data)


class AssetUpdateView(AssetWrite, ServiceUpdateMixin, LoginRequiredMixin, UpdateView):
    form_class = AssetForm
    template_name = "assets/asset_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    service = staticmethod(save_asset)
    command_from_cleaned_data = staticmethod(asset_command_from_cleaned_data)


class AssetDeleteView(AssetWrite, ServiceDeleteMixin, LoginRequiredMixin, DeleteView):
    template_name = "assets/asset_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("assets:list")
    context_object_name = "asset"
    service = staticmethod(delete_asset)
