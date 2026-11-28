"""The content resource, declared on its domain in application/domains.py."""

from typing import Annotated, TypedDict

from django.db.models import Q

from hq.domains.docs_index.models import DocumentationRecord
from hq.platform.application import content
from hq.platform.application.answer_fields import (
    AddedAt,
    LinkedAssets,
    LinkedDocs,
    LinkedProjects,
    PageUrl,
    RecordedExpenses,
    RecordSlug,
    UpdatedAt,
)
from hq.platform.application.describe import Describe
from hq.platform.application.integration_specs import (
    DetailRead,
    ListFilter,
    ListRead,
    ListSort,
    ListToggle,
    ResourceSpec,
)
from hq.platform.application.record_fields import Notes, PublishedAt, PublishedUrl
from hq.platform.application.resources import BoundedQuery, SearchText
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import ContentItem


class ContentQuery(BoundedQuery):
    query: SearchText = None


def _undocumented() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A writeup no document names, kept, and one a runbook is the source of, left out."""

    ContentItem.objects.create(title="Bare", slug="bare")
    sourced = ContentItem.objects.create(title="Sourced", slug="sourced")
    sourced.related_documentation.add(DocumentationRecord.objects.create(doc_id="rb-example", title="Example runbook"))
    return ("bare",), ("sourced",)


FILTERS = (
    ListFilter("status", "Status", ContentItem.Status.choices),
    ListFilter("content_type", "Type", ContentItem.Type.choices),
)
NO_DOCS = ListToggle(
    "no_docs",
    "No source document",
    "True for only the writeups and pages no document names.",
    Q(related_documentation=None),
    _undocumented,
)

# Each order the list is read in, the first where none is asked.
SORTS = (
    *ListSort.both("-published_at", "Recently published", "Oldest published"),
    *ListSort.both("title", "Title"),
    *ListSort.both("content_type", "Type"),
    *ListSort.both("status", "Status"),
)


class ContentRow(TypedDict):
    """One writeup, video or page, published or not."""

    slug: RecordSlug
    url: PageUrl
    title: content.ContentTitle
    content_type: content.ContentType
    status: content.ContentStatus
    topic: content.ContentTopic
    tags: Annotated[list[str], Describe("The tags it is filed under, in the order they were written.")]
    published_url: PublishedUrl
    published_at: PublishedAt
    notes: Notes
    created_at: AddedAt
    updated_at: UpdatedAt


class ContentLinks(TypedDict):
    """The records a writeup or page is linked to."""

    projects: LinkedProjects
    assets: LinkedAssets
    expense_ids: RecordedExpenses
    documentation: LinkedDocs


class ContentRecord(ContentRow):
    """One writeup, video or page with the records it is linked to."""

    relationships: ContentLinks


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "content",
            "Content",
            "Content records indexed by HQ.",
            Capability.READ,
            listing=ListRead(
                content.list_content,
                ContentQuery,
                ContentRow,
                sorts=SORTS,
                filters=FILTERS,
                toggles=(NO_DOCS,),
            ),
            detail=DetailRead(content.get_content, ContentRecord),
            identifier="slug",
            search=SearchDefinition.of(
                ContentItem,
                "slug",
                ("title", "slug", "topic", "tags", "notes"),
                timestamp_field="published_at",
                title_field="title",
            ),
            web_route="content:list",
        ),
    )
