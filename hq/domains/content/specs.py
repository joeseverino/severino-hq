"""The content resource, declared on its domain in application/domains.py."""

from typing import Annotated, TypedDict

from pydantic import Field, create_model

from hq.platform.application import content
from hq.platform.application.answer_fields import RecordSlug, UpdatedAt
from hq.platform.application.describe import Describe, listed
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.record_fields import Notes, PublishedUrl
from hq.platform.application.resources import STATUS_FILTER, BoundedQuery, TextFilter, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability
from hq.platform.application.ui import ended

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
    title: content.ContentTitle
    content_type: Annotated[str, Describe(ended(f"What form it takes: {listed(ContentItem.Type.values)}"))]
    status: Annotated[str, Describe(ended(f"How far along it is: {listed(ContentItem.Status.values)}"))]
    topic: Annotated[str, Describe("What it is about, in a few words; empty when not recorded.")]
    tags: Annotated[list[str], Describe("The tags it is filed under, in the order they were written.")]
    published_url: PublishedUrl
    published_at: Annotated[str | None, Describe("When it was published, an ISO 8601 date; null when not recorded.")]
    notes: Notes
    updated_at: UpdatedAt


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "content",
            "Content",
            "Content records indexed by HQ.",
            Capability.READ,
            content.list_content,
            ContentQuery,
            search=SearchDefinition(
                "content",
                ContentItem,
                "slug",
                ("title", "slug", "topic", "tags", "notes"),
                label="Content",
                title_field="title",
            ),
            web_route="content:list",
            row_type=ContentRow,
        ),
    )
