from typing import override

from django.urls import reverse_lazy
from django.utils.functional import cached_property
from django.views.generic import (
    CreateView,
    DeleteView,
    TemplateView,
    UpdateView,
)

from hq.platform.application import related
from hq.platform.application.analytics import CONTENT_TRAFFIC_DAYS, attach_traffic, item_traffic
from hq.platform.application.content import published_on
from hq.platform.application.documentation import related_documents
from hq.platform.application.entity_links import entity_link, web_url
from hq.platform.application.labels import adding, dotted_html
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.moments import moment, stamp_day
from hq.platform.application.pages import PageAction, PageMixin, RecordPageMixin
from hq.platform.application.tables import Cell, TableColumn
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin
from hq.platform.core.templatetags.status_tags import status_pill

from .models import PAGE_TYPES, WRITEUP_TYPES, ContentItem


def _live(row) -> Cell:
    """The item's live page, where it is published at a web address."""

    url = web_url(row["published_url"])
    return Cell("Live", url, external=True) if url else Cell()


class _ContentSectionView(ListPageMixin, TemplateView):
    """One half of the registry, as a table.

    The registry is cut once, in ``content.models``. A section states which half
    it is and its heading; the rest of the table is the content list's own, read
    within the section's types, so the two cannot drift apart.
    """

    resource = "content"
    template_name = "content/content_list.html"
    content_types: frozenset[str] = frozenset()
    heading = ""
    # What the head's button and the empty list call a new item here, and
    # the type its form opens on.
    # The words its new-item button says, a variant of content.create.
    new_variant = "writeup_or_page"
    new_type = ""
    table_empty = "No writeups or pages yet."
    table_selectable = True
    table_search_placeholder = "Search titles, topics, tags, and notes…"

    @override
    def get_page_title(self):
        return self.heading

    @override
    def get_page_actions(self):
        # The new-item form, opened on this section's type when it has one.
        opened = {"content_type": self.new_type} if self.new_type else None
        return (PageAction.command("content.create", primary=True, variant=self.new_variant, query=opened),)

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

    @override
    def scope(self):
        # Only the types this half holds: one it is defined to exclude would
        # give back an empty table, which reads as "nothing published" rather
        # than as "wrong section". A section all of one type shows neither the
        # Type column nor its filter.
        return {"content_type": tuple(sorted(self.types_held or self.content_types))}

    @cached_property
    def table_columns(self) -> tuple[TableColumn, ...]:
        typed = (
            (TableColumn("Type", "content_type", drop=6, cell=lambda row: Cell(row["type_label"])),)
            if len(self.types_held) > 1
            else ()
        )
        return (
            TableColumn(
                "Title",
                "title",
                cell=lambda row: Cell.of(entity_link("writeup", row["slug"], label=row["title"])),
            ),
            *typed,
            TableColumn("Status", "status", css="key-col", cell=lambda row: Cell.state(row["state"])),
            TableColumn("Description", drop=1, cell=lambda row: Cell(row["topic"] or "", muted=True)),
            TableColumn(
                f"Views ({CONTENT_TRAFFIC_DAYS} days)",
                kind="numeric",
                drop=5,
                cell=lambda row: Cell("" if row["pageviews"] is None else str(row["pageviews"])),
            ),
            TableColumn(
                "Published",
                "published_at",
                drop=3,
                cell=lambda row: Cell(moment=stamp_day(row["published_at"]), muted=True),
            ),
            TableColumn(
                "Updated",
                "updated_at",
                drop=2,
                cell=lambda row: Cell(moment=moment(row["updated_at"]), muted=True),
            ),
            TableColumn("Live page", css="actions-col", drop=4, cell=_live),
        )

    @override
    def row(self, item):
        return {
            **item,
            "state": ContentItem.Status(item["status"]),
            "type_label": ContentItem.Type(item["content_type"]).label,
        }

    @override
    def rows(self, items):
        # Traffic for the rows on this page only, in one query whatever its size.
        return attach_traffic(super().rows(items))

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Where the section is published, said once when it is all one site.
        context["site"] = published_on(url for _type, url in self._held)
        return context


class WriteupListView(_ContentSectionView):
    content_types = WRITEUP_TYPES
    heading = "Writeups"
    new_variant = "writeup"
    new_type = ContentItem.Type.ARTICLE.value
    table_empty = "No writeups yet."


class PageListView(_ContentSectionView):
    content_types = PAGE_TYPES
    heading = "Pages"
    new_variant = "page"
    new_type = ContentItem.Type.PAGE.value
    table_empty = "No pages yet."


class ContentListView(_ContentSectionView):
    """Both halves at once.

    The draft and needs-docs queues count across the whole registry, so they
    open here, where the number and the page agree; and a type no section
    claims (a video, say) stays visible here.
    """

    content_types = frozenset(ContentItem.Type)
    heading = "Content"


CONTENT_TRAIL = (("Content", reverse_lazy("content:list")),)


class ContentPage(PageMixin):
    """A page about one content item, or a new one: its trail runs back to the list."""

    model = ContentItem
    page_trail = CONTENT_TRAIL

    @override
    def trail_label(self, record):
        return record.title


class ContentDetailView(RecordPageMixin, TemplateView):
    page_trail = CONTENT_TRAIL
    template_name = "content/content_detail.html"
    resource = "content"
    context_object_name = "item"
    model = ContentItem
    stored_related = ("related_projects", "related_assets", "related_documentation", "related_expenses")

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        item = self.stored
        documents = related_documents(
            item.related_documentation.all(), listed=item.related_projects.all(), sources=True
        )
        return context | {
            "traffic": item_traffic(self.record["published_url"]),
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
        return self.record["title"]

    @override
    def get_page_lede(self):
        return dotted_html(
            ContentItem.Type(self.record["content_type"]).label,
            status_pill(ContentItem.Status(self.record["status"])),
        )

    @override
    def get_page_actions(self):
        record = self.record
        actions = []
        if record["published_url"]:
            actions.append(PageAction("Open live page", record["published_url"]))
        actions += [
            PageAction.command("content.update", record["slug"]),
            PageAction.command("content.delete", record["slug"]),
        ]
        return tuple(actions)


def noun_of(content_type: str) -> str:
    """What one item is called: a page or a writeup, as the two lists name them."""

    return "page" if content_type in PAGE_TYPES else "writeup"


class ContentCreateView(ContentPage, RecordFormMixin, CreateView):
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
    def get_form_verb(self, form):
        return adding(noun_of(self.chosen_type) if self.chosen_type else "writeup or page")


class ContentUpdateView(ContentPage, RecordFormMixin, UpdateView):
    @override
    def get_page_title(self):
        return f"Edit {noun_of(self.object.content_type)}"


class ContentDeleteView(ContentPage, RecordDeleteMixin, DeleteView):
    template_name = "content/content_confirm_delete.html"

    @override
    def get_page_title(self):
        return f"Delete {noun_of(self.object.content_type)}?"

    success_url = reverse_lazy("content:list")
    context_object_name = "item"
