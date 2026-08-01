from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from application.assets import asset_command_from_cleaned_data, save_asset
from application.deletion import DeleteCommand, delete_asset
from application.security import web_principal
from application.tables import TableFilter, TableListMixin, TableSort, TableToggle
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


class AssetCreateView(LoginRequiredMixin, CreateView):
    model = Asset
    form_class = AssetForm
    template_name = "assets/asset_form.html"

    def form_valid(self, form):
        result = save_asset(
            asset_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
        )
        self.object = Asset.objects.get(slug=result["asset"]["slug"])
        messages.success(self.request, f"Asset “{self.object}” created.")
        return redirect(self.object.get_absolute_url())


class AssetUpdateView(LoginRequiredMixin, UpdateView):
    model = Asset
    form_class = AssetForm
    template_name = "assets/asset_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def form_valid(self, form):
        result = save_asset(
            asset_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
            current_slug=self.get_object().slug,
        )
        self.object = Asset.objects.get(slug=result["asset"]["slug"])
        messages.success(self.request, f"Asset “{self.object}” updated.")
        return redirect(self.object.get_absolute_url())


class AssetDeleteView(LoginRequiredMixin, DeleteView):
    model = Asset
    template_name = "assets/asset_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("assets:list")
    context_object_name = "asset"

    def form_valid(self, form):
        slug = self.get_object().slug
        result = delete_asset(
            DeleteCommand(confirm=slug),
            principal=web_principal(self.request.user),
            current_slug=slug,
        )
        messages.success(self.request, f"Asset “{result['deleted']['label']}” deleted.")
        return redirect(self.success_url)
