from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from application.content import content_command_from_cleaned_data, save_content
from application.deletion import delete_content
from application.tables import TableFilter, TableListMixin, TableSort, TableToggle
from application.writes import (
    ServiceCreateMixin,
    ServiceDeleteMixin,
    ServiceUpdateMixin,
)
from .forms import ContentItemForm
from .models import ContentItem


class ContentListView(TableListMixin, LoginRequiredMixin, ListView):
    model = ContentItem
    template_name = "content/content_list.html"
    context_object_name = "items"
    paginate_by = 25
    table_search_scope = "content"
    table_filters = (
        TableFilter("status", "Status", "status", ContentItem.Status.choices),
        TableFilter("content_type", "Type", "content_type", ContentItem.Type.choices),
    )
    table_sorts = (
        TableSort("-updated_at", "Recently updated", "-updated_at"),
        TableSort("title", "Title A–Z", "title"),
        TableSort("-title", "Title Z–A", "-title"),
        TableSort("-published_at", "Recently published", "-published_at"),
        TableSort("published_at", "Oldest published", "published_at"),
        TableSort("status", "Status", "status"),
        TableSort("-status", "Status reverse", "-status"),
        TableSort("content_type", "Type", "content_type"),
        TableSort("-content_type", "Type reverse", "-content_type"),
        TableSort("updated_at", "Least recently updated", "updated_at"),
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


class ContentWrite:
    """What every content write shares, whichever direction it goes."""

    model = ContentItem
    noun = "Content item"
    result_key = "content"
    identity_attr = "slug"
    identity_kwarg = "current_slug"


class ContentCreateView(
    ContentWrite, ServiceCreateMixin, LoginRequiredMixin, CreateView
):
    form_class = ContentItemForm
    template_name = "content/content_form.html"
    service = staticmethod(save_content)
    command_from_cleaned_data = staticmethod(content_command_from_cleaned_data)


class ContentUpdateView(
    ContentWrite, ServiceUpdateMixin, LoginRequiredMixin, UpdateView
):
    form_class = ContentItemForm
    template_name = "content/content_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    service = staticmethod(save_content)
    command_from_cleaned_data = staticmethod(content_command_from_cleaned_data)


class ContentDeleteView(
    ContentWrite, ServiceDeleteMixin, LoginRequiredMixin, DeleteView
):
    template_name = "content/content_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("content:list")
    context_object_name = "item"
    service = staticmethod(delete_content)
