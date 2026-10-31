from typing import override

from django.db.models import Count
from django.urls import reverse, reverse_lazy
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from hq.platform.application import related
from hq.platform.application.analytics import CONTENT_TRAFFIC_DAYS, attach_traffic, item_traffic
from hq.platform.application.capabilities import capability_words
from hq.platform.application.content import published_on
from hq.platform.application.documentation import related_documents
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.tables import TableColumn, TableFilter, TableListMixin, TableToggle
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin

from .forms import ContentItemForm
from .models import PAGE_TYPES, WRITEUP_TYPES, ContentItem


class _ContentSectionView(PageMixin, TableListMixin, ListView):
    """One half of the registry, as a table.

    The registry is cut once, in ``content.models``. A section states which half
    it is and its heading; every other part of the table contract (search scope,
    sorts, toggles, paging) is shared, so the two cannot drift apart.
    """

    model = ContentItem
    template_name = "content/content_list.html"
    context_object_name = "items"
    paginate_by = 25
    table_search_scope = "content"
    content_types: frozenset[str] = frozenset()
    heading = ""
    # What the head's button and the empty list call a new item here, and
    # the type its form opens on.
    new_label = "New writeup or page"
    new_type = ""
    table_empty = "No writeups or pages yet."
    table_filters = (TableFilter("status", "Status", "status", ContentItem.Status.choices),)
    table_selectable = True
    table_toggles = (TableToggle("no_docs", "No source document"),)
    table_default_sort = "-updated_at"
    table_search_placeholder = "Search titles, topics, tags, and notes…"

    @override
    def get_page_title(self):
        return self.heading

    @cached_property
    def new_url(self) -> str:
        """The new-item form, opened on this section's type when it has one."""

        query = f"?content_type={self.new_type}" if self.new_type else ""
        return f"{reverse('content:create')}{query}"

    @override
    def get_page_actions(self):
        return (PageAction(self.new_label, self.new_url, primary=True),)

    @cached_property
    def _held(self) -> tuple[tuple[str, str], ...]:
        """Each type this section holds with each address one is published at, in one read."""

        return tuple(
            ContentItem.objects.filter(content_type__in=self.content_types)
            .order_by()
            .values_list("content_type", "published_url")
            .distinct()
        )

    @cached_property
    def types_held(self) -> frozenset[str]:
        return frozenset(content_type for content_type, _url in self._held)

    @cached_property
    def table_columns(self) -> tuple[TableColumn, ...]:
        typed = (TableColumn("Type", "content_type", drop=6),) if len(self.types_held) > 1 else ()
        return (
            TableColumn("Title", "title"),
            *typed,
            TableColumn("Status", "status", css="key-col"),
            TableColumn("Description", drop=1),
            TableColumn(f"Views ({CONTENT_TRAFFIC_DAYS} days)", css="num-col", drop=5),
            TableColumn("Published", "published_at", "Oldest published", "Recently published", drop=3),
            TableColumn("Updated", "updated_at", "Least recently updated", "Recently updated", drop=2),
            TableColumn("Live page", css="actions-col", drop=4),
        )

    @override
    def get_table_filters(self):
        # Only the types this half holds: one it is defined to exclude would
        # give back an empty table, which reads as "nothing published" rather
        # than as "wrong section". A section all of one type shows neither the
        # Type column nor its filter.
        if len(self.types_held) < 2:
            return self.table_filters
        return (
            *self.table_filters,
            TableFilter(
                "content_type",
                "Type",
                "content_type",
                [(value, label) for value, label in ContentItem.Type.choices if value in self.types_held],
            ),
        )

    @override
    def get_queryset(self):
        qs = ContentItem.objects.filter(content_type__in=self.content_types)
        if self.request.GET.get("no_docs"):
            qs = qs.annotate(doc_count=Count("related_documentation")).filter(doc_count=0)
        return self.apply_table_query(qs)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Traffic for the rows on this page only. Joined after pagination, so
        # the reading is one query whatever the page size, and the table engine
        # keeps ownership of which rows and in what order.
        attach_traffic(context["items"])
        context.update(
            # Where the section is published, said once when it is all one site.
            site=published_on(url for _type, url in self._held),
            show_type=len(self.types_held) > 1,
            new_label=self.new_label,
            new_url=self.new_url,
        )
        return context


class WriteupListView(_ContentSectionView):
    content_types = WRITEUP_TYPES
    heading = "Writeups"
    new_label = "New writeup"
    new_type = ContentItem.Type.ARTICLE.value
    table_empty = "No writeups yet."


class PageListView(_ContentSectionView):
    content_types = PAGE_TYPES
    heading = "Pages"
    new_label = "New page"
    new_type = ContentItem.Type.PAGE.value
    table_empty = "No pages yet."


class ContentListView(_ContentSectionView):
    """Both halves at once. Reachable, but not in the nav.

    The draft and needs-docs queues count across the whole registry, so they
    open here, where the number and the page agree; and a type no section
    claims (a video, say) stays visible here.
    """

    content_types = frozenset(ContentItem.Type)
    heading = "Content"


CONTENT_TRAIL = ("Content", reverse_lazy("content:list"))


class ContentPage(PageMixin):
    """A page about one content item, or a new one: its trail runs back to the list."""

    page_trail = CONTENT_TRAIL

    @override
    def trail_label(self, record):
        return record.title


class ContentDetailView(PageMixin, DetailView):
    model = ContentItem
    page_trail = CONTENT_TRAIL
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

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        item = context["item"]
        documents = related_documents(
            item.related_documentation.all(), listed=item.related_projects.all(), sources=True
        )
        return context | {
            "traffic": item_traffic(item),
            "related": related.related(
                related.links("Source document", documents.sources),
                related.projects(item.related_projects.all(), documents.projects),
                related.assets(item.related_assets.all()),
                related.expenses(item.related_expenses.all()),
                *documents.groups,
            ),
        }

    @override
    def get_page_title(self):
        return self.object.title

    @override
    def get_page_lede(self):
        return format_html(
            '{} · <span class="pill pill-{}">{}</span>',
            self.object.get_content_type_display(),
            self.object.status,
            self.object.get_status_display(),
        )

    @override
    def get_page_actions(self):
        item = self.object
        actions = []
        if item.published_url:
            actions.append(PageAction("Open live page", item.published_url))
        actions += [
            PageAction(capability_words("content.update"), reverse("content:edit", args=[item.slug])),
            PageAction("Delete", reverse("content:delete", args=[item.slug]), danger=True),
        ]
        return tuple(actions)


def noun_of(content_type: str) -> str:
    """What one item is called: a page or a writeup, as the two lists name them."""

    return "page" if content_type in PAGE_TYPES else "writeup"


class ContentCreateView(ContentPage, RecordFormMixin, CreateView):
    form_class = ContentItemForm
    template_name = "content/content_form.html"

    @cached_property
    def chosen_type(self) -> str:
        """The type the list this form was opened from holds, when it named one."""

        asked = self.request.GET.get("content_type", "")
        return asked if asked in ContentItem.Type.values else ""

    @override
    def get_initial(self):
        initial = super().get_initial()
        if self.chosen_type:
            initial["content_type"] = self.chosen_type
        return initial

    @override
    def get_page_title(self):
        return f"New {noun_of(self.chosen_type)}" if self.chosen_type else "New writeup or page"

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["noun"] = noun_of(self.chosen_type) if self.chosen_type else "writeup or page"
        return context


class ContentUpdateView(ContentPage, RecordFormMixin, UpdateView):
    model = ContentItem
    form_class = ContentItemForm
    template_name = "content/content_form.html"

    @override
    def get_page_title(self):
        return f"Edit {noun_of(self.object.content_type)}"


class ContentDeleteView(ContentPage, RecordDeleteMixin, DeleteView):
    model = ContentItem
    template_name = "content/content_confirm_delete.html"

    @override
    def get_page_title(self):
        return f"Delete {noun_of(self.object.content_type)}?"

    success_url = reverse_lazy("content:list")
    context_object_name = "item"
