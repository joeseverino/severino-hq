from django.contrib import messages
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

from .forms import AssetForm
from .models import ASSET_CATEGORY_CHOICES, Asset


class AssetListView(LoginRequiredMixin, ListView):
    model = Asset
    template_name = "assets/asset_list.html"
    context_object_name = "assets_list"
    paginate_by = 25

    def get_queryset(self):
        qs = Asset.objects.all()
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "").strip()
        category = self.request.GET.get("category", "").strip()
        sort = self.request.GET.get("sort", "-purchase_date")
        if q:
            qs = qs.filter(
                Q(item_name__icontains=q)
                | Q(vendor__icontains=q)
                | Q(serial_number__icontains=q)
                | Q(notes__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)
        if category:
            qs = qs.filter(category=category)
        if sort in {
            "item_name", "-item_name",
            "purchase_date", "-purchase_date",
            "total_cost", "-total_cost",
            "status", "-status",
            "category", "-category",
        }:
            qs = qs.order_by(sort)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            q=self.request.GET.get("q", ""),
            selected_status=self.request.GET.get("status", ""),
            selected_category=self.request.GET.get("category", ""),
            sort=self.request.GET.get("sort", "-purchase_date"),
            status_choices=Asset.Status.choices,
            category_choices=ASSET_CATEGORY_CHOICES,
        )
        return ctx


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
        response = super().form_valid(form)
        messages.success(self.request, f"Asset “{self.object}” created.")
        return response


class AssetUpdateView(LoginRequiredMixin, UpdateView):
    model = Asset
    form_class = AssetForm
    template_name = "assets/asset_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Asset “{self.object}” updated.")
        return response


class AssetDeleteView(LoginRequiredMixin, DeleteView):
    model = Asset
    template_name = "assets/asset_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("assets:list")
    context_object_name = "asset"

    def form_valid(self, form):
        name = str(self.get_object())
        response = super().form_valid(form)
        messages.success(self.request, f"Asset “{name}” deleted.")
        return response
