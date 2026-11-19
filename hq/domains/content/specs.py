"""The content resource, declared on its domain in application/domains.py."""

from typing import Annotated, TypedDict

from pydantic import Field, create_model

from hq.platform.application import content
from hq.platform.application.answer_fields import (
    AddedAt,
    LinkedAssets,
    LinkedDocs,
    LinkedProjects,
    RecordedExpenses,
    RecordSlug,
    UpdatedAt,
)
from hq.platform.application.describe import Describe
from hq.platform.application.integration_specs import ListRead, ResourceSpec
from hq.platform.application.record_fields import Notes, PublishedAt, PublishedUrl
from hq.platform.application.resources import STATUS_FILTER, BoundedQuery, TextFilter, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import ContentItem

ContentQuery = create_model(
    "ContentQuery",
    __base__=BoundedQuery,
    status=(one_of(ContentItem.Status.values) | None, Field(default=None, description=STATUS_FILTER)),
    query=(TextFilter, None),
)


class ContentRow(TypedDict):
    """One writeup, video or page, published or not."""

    slug: RecordSlug
    url: Annotated[str, Describe("The writeup's or page's page in HQ.")]
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
            listing=ListRead(content.list_content, ContentQuery, ContentRow),
            detail_handler=content.get_content,
            identifier="slug",
            search=SearchDefinition(
                "content",
                ContentItem,
                "slug",
                ("title", "slug", "topic", "tags", "notes"),
                label="Content",
                title_field="title",
            ),
            web_route="content:list",
            record_type=ContentRecord,
        ),
    )
