"""The content resource, declared on its domain in application/domains.py."""

from typing import TypedDict

from pydantic import Field, create_model

from hq.platform.application import content
from hq.platform.application.integration_specs import ResourceSpec
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
    slug: str
    title: str
    content_type: str
    status: str
    topic: str
    tags: list[str]
    published_url: str
    published_at: str | None
    notes: str
    updated_at: str


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
