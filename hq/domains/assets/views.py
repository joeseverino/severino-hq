from django.db.models import Q, Sum
from django.urls import reverse, reverse_lazy
from django.utils.html import format_html
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from hq.platform.application.assets import managed_domain
from hq.platform.application.documentation import related_documents
from hq.platform.application.expenses import cost_totals
from hq.platform.application.pages import PageAction, PageMixin, record_trail
from hq.platform.application.tables import TableColumn, TableFilter, TableListMixin, TableToggle
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin
from .forms import AssetForm
from .models import ASSET_CATEGORY_CHOICES, Asset


class AssetListView(PageMixin, TableListMixin, ListView):
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

    def get_page_actions(self):
        return (PageAction("New asset", reverse("assets:create"), primary=True),)

    def get_queryset(self):
        qs = Asset.objects.all()
        if self.request.GET.get("missing_purchase"):
            qs = qs.filter(status=Asset.Status.ACTIVE).filter(
                Q(purchase_date__isnull=True) | Q(total_cost=0)
            )
        return self.apply_table_query(qs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        totals = self.object_list.aggregate(
            total=Sum("total_cost"), deductible=Sum("estimated_deductible_amount")
        )
        context["totals"] = cost_totals(totals, narrowed=bool(context["table"]["active_count"]))
        return context


class AssetPage(PageMixin):
    """A page about one asset, or a new one: its trail runs back to the list."""

    def get_page_trail(self):
        return record_trail(
            ("Assets", reverse("assets:list")),
            getattr(self, "object", None),
            lambda asset: asset.item_name,
        )


class AssetDetailView(PageMixin, DetailView):
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        asset = self.object
        context["documents"] = related_documents(
            asset.documentation_records.all(), listed=asset.related_projects.all()
        )
        context["domain"] = managed_domain(asset)
        return context

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


class AssetCreateView(AssetPage, RecordFormMixin, CreateView):
    page_title = "New asset"
    form_class = AssetForm
    template_name = "assets/asset_form.html"


class AssetUpdateView(AssetPage, RecordFormMixin, UpdateView):
    page_title = "Edit asset"
    model = Asset
    form_class = AssetForm
    template_name = "assets/asset_form.html"


class AssetDeleteView(AssetPage, RecordDeleteMixin, DeleteView):
    page_title = "Delete asset?"
    model = Asset
    template_name = "assets/asset_confirm_delete.html"
    success_url = reverse_lazy("assets:list")
    context_object_name = "asset"
