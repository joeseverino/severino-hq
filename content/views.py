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

from application.analytics import CONTENT_TRAFFIC_DAYS, attach_traffic, item_traffic
from application.content import content_command_from_cleaned_data, save_content
from application.deletion import delete_content
from application.tables import TableFilter, TableListMixin, TableSort, TableToggle
from application.writes import (
    ServiceCreateMixin,
    ServiceDeleteMixin,
    ServiceUpdateMixin,
)
from .forms import ContentItemForm
from .models import PAGE_TYPES, WRITEUP_TYPES, ContentItem


class _ContentSectionView(TableListMixin, LoginRequiredMixin, ListView):
    """One half of the registry, as a table.

    The registry is cut once, in ``content.models``, and both sections read the
    cut from there. A section states which half it is and its heading; every
    other part of the table contract -- search scope, sorts, toggles, paging --
    is shared, so the two cannot drift into behaving differently.

    The type filter offers only the types its own half can contain. Offering all
    nine would let an operator select a type this section is defined to exclude
    and get an empty table back, which reads as "nothing published" rather than
    as "wrong section".
    """

    model = ContentItem
    template_name = "content/content_list.html"
    context_object_name = "items"
    paginate_by = 25
    table_search_scope = "content"
    content_types: frozenset[str] = frozenset()
    heading = ""
    table_filters = (
        TableFilter("status", "Status", "status", ContentItem.Status.choices),
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

    def get_table_filters(self):
        return (
            *self.table_filters,
            TableFilter(
                "content_type",
                "Type",
                "content_type",
                [
                    (value, label)
                    for value, label in ContentItem.Type.choices
                    if value in self.content_types
                ],
            ),
        )

    def get_queryset(self):
        qs = ContentItem.objects.filter(content_type__in=self.content_types)
        if self.request.GET.get("no_docs"):
            qs = qs.annotate(doc_count=Count("related_documentation")).filter(
                doc_count=0
            )
        return self.apply_table_query(qs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Traffic for the rows on this page only. Joined after pagination, so
        # the reading is one query whatever the page size, and the table engine
        # keeps ownership of which rows and in what order.
        attach_traffic(context["items"])
        return context | {
            "heading": self.heading,
            "traffic_days": CONTENT_TRAFFIC_DAYS,
        }


class WriteupListView(_ContentSectionView):
    content_types = WRITEUP_TYPES
    heading = "Writeups"


class PageListView(_ContentSectionView):
    content_types = PAGE_TYPES
    heading = "Pages"


class ContentListView(_ContentSectionView):
    """Both halves at once. Reachable, but not in the nav.

    The nav offers the two sections, because those are the two jobs. This is
    where a question that spans them lands: the draft queue and the needs-docs
    queue both count across the whole registry, and pointing either at one
    section would make the number and the page it opens disagree.

    It is also where a content type that is neither -- a video, say -- stays
    visible while there is no section that claims it.
    """

    content_types = frozenset(ContentItem.Type)
    heading = "Content pipeline"


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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return context | {"traffic": item_traffic(context["item"])}


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
