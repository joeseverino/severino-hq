from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from application.content import content_command_from_cleaned_data, save_content
from application.deletion import DeleteCommand, delete_content
from application.security import web_principal
from application.tables import TableFilter, TableListMixin, TableSort, TableToggle
from .forms import ContentItemForm
from .models import ContentItem


class ContentListView(TableListMixin, LoginRequiredMixin, ListView):
    model = ContentItem
    template_name = "content/content_list.html"
    context_object_name = "items"
    paginate_by = 25
    table_search_fields = ("title", "topic", "tags", "notes")
    table_filters = (
        TableFilter("status", "Status", "status", ContentItem.Status.choices),
        TableFilter("content_type", "Type", "content_type", ContentItem.Type.choices),
    )
    table_sorts = (
        TableSort("-updated_at", "Recently updated", "-updated_at"),
        TableSort("title", "Title A–Z", "title"),
        TableSort("-published_at", "Recently published", "-published_at"),
        TableSort("status", "Status", "status"),
    )
    table_toggles = (TableToggle("no_docs", "Missing documentation"),)
    table_default_sort = "-updated_at"
    table_search_placeholder = "Search titles, topics, tags, and notes…"

    def get_queryset(self):
        qs = ContentItem.objects.all()
        if self.request.GET.get("no_docs"):
            qs = qs.annotate(doc_count=Count("related_documentation")).filter(
                doc_count=0
            )
        return self.apply_table_query(qs)

class ContentDetailView(LoginRequiredMixin, DetailView):
    model = ContentItem
    template_name = "content/content_detail.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    context_object_name = "item"
    queryset = ContentItem.objects.prefetch_related(
        "related_projects",
        "related_assets",
        "related_documentation",
        "related_expenses",
    )


class ContentCreateView(LoginRequiredMixin, CreateView):
    model = ContentItem
    form_class = ContentItemForm
    template_name = "content/content_form.html"

    def form_valid(self, form):
        result = save_content(
            content_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
        )
        self.object = ContentItem.objects.get(slug=result["content"]["slug"])
        messages.success(self.request, f"Content item “{self.object}” created.")
        return redirect(self.object.get_absolute_url())


class ContentUpdateView(LoginRequiredMixin, UpdateView):
    model = ContentItem
    form_class = ContentItemForm
    template_name = "content/content_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def form_valid(self, form):
        result = save_content(
            content_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
            current_slug=self.get_object().slug,
        )
        self.object = ContentItem.objects.get(slug=result["content"]["slug"])
        messages.success(self.request, f"Content item “{self.object}” updated.")
        return redirect(self.object.get_absolute_url())


class ContentDeleteView(LoginRequiredMixin, DeleteView):
    model = ContentItem
    template_name = "content/content_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("content:list")
    context_object_name = "item"

    def form_valid(self, form):
        slug = self.get_object().slug
        result = delete_content(
            DeleteCommand(confirm=slug),
            principal=web_principal(self.request.user),
            current_slug=slug,
        )
        messages.success(self.request, f"Content item “{result['deleted']['label']}” deleted.")
        return redirect(self.success_url)
