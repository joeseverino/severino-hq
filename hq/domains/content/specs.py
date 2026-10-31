"""The content resource, declared on its domain in application/domains.py."""

from hq.platform.application import content
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import BoundedQuery
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import ContentItem


class ContentQuery(BoundedQuery):
    status: str | None = None
    query: str | None = None


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
        ),
    )
